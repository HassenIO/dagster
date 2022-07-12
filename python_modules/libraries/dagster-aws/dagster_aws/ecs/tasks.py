import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from dagster.utils import merge_dicts
from dagster.utils.backoff import backoff


@dataclass
class DagsterEcsTaskConfig:
    """All the information needs that Dagster needs to launch an ECS task."""

    image: str
    container_name: str
    command: Optional[str]
    family: str
    cluster: str
    subnets: List[str]
    security_groups: List[str]
    execution_role_arn: Optional[str]
    task_role_arn: Optional[str]
    assign_public_ip: bool
    secrets: Optional[List[Dict[str, str]]]
    environment: Optional[List[Dict[str, str]]]
    log_configuration: Optional[Dict[str, str]]

    def network_configuration(self):
        return {
            "awsvpcConfiguration": {
                "subnets": self.subnets,
                "assignPublicIp": self.assign_public_ip,
                "securityGroups": self.security_groups,
            }
        }

    def task_definition(self):
        kwargs = dict(
            family=self.family,
            requiresCompatibilities=["FARGATE"],
            networkMode="awsvpc",
            containerDefinitions=[
                merge_dicts(
                    {
                        "name": self.container_name,
                        "image": self.image,
                    },
                    (
                        {"logConfiguration": self.log_configuration}
                        if self.log_configuration
                        else {}
                    ),
                    ({"command": self.command} if self.command else {}),
                    ({"secrets": self.secrets} if self.secrets else {}),
                    ({"environment": self.environment} if self.environment else {}),
                )
            ],
            executionRoleArn=self.execution_role_arn,
            # https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-cpu-memory-error.html
            cpu="256",
            memory="512",
        )

        if self.task_role_arn:
            kwargs.update(dict(taskRoleArn=self.task_role_arn))

        return kwargs


# 9 retries polls for up to 51.1 seconds with exponential backoff.
BACKOFF_RETRIES = 9


# The ECS API is eventually consistent:
# https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_RunTask.html
# describe_tasks might initially return nothing even if a task exists.
class EcsEventualConsistencyTimeout(Exception):
    pass


class EcsNoTasksFound(Exception):
    pass


def default_ecs_task_definition(
    ecs,
    family,
    current_task_definition,
    image,
    container_name,
    environment,
    command=None,
    secrets=None,
    include_sidecars=False,
):

    current_container_name = current_ecs_container_name()

    container_definition = next(
        iter(
            [
                container
                for container in current_task_definition["containerDefinitions"]
                if container["name"] == current_container_name
            ]
        )
    )

    # Start with the current process's task's definition but remove
    # extra keys that aren't useful for creating a new task definition
    # (status, revision, etc.)
    expected_keys = [
        key for key in ecs.meta.service_model.shape_for("RegisterTaskDefinitionRequest").members
    ]
    task_definition = dict(
        (key, current_task_definition[key])
        for key in expected_keys
        if key in current_task_definition.keys()
    )

    # The current process might not be running in a container that has the
    # pipeline's code installed. Inherit most of the process's container
    # definition (things like environment, dependencies, etc.) but replace
    # the image with the pipeline origin's image and give it a new name.
    # Also remove entryPoint. We plan to set containerOverrides. If both
    # entryPoint and containerOverrides are specified, they're concatenated
    # and the command will fail
    # https://aws.amazon.com/blogs/opensource/demystifying-entrypoint-cmd-docker/
    new_container_definition = merge_dicts(
        {
            **container_definition,
            "name": container_name,
            "image": image,
            "entryPoint": [],
            "command": command if command else [],
        },
        ({"environment": environment} if environment else {}),
        ({"secrets": secrets} if secrets else {}),
        {} if include_sidecars else {"dependsOn": []},
    )

    if include_sidecars:
        container_definitions = current_task_definition.get("containerDefinitions")
        container_definitions.remove(container_definition)
        container_definitions.append(new_container_definition)
    else:
        container_definitions = [new_container_definition]

    task_definition = {
        **task_definition,
        "family": family,
        "containerDefinitions": container_definitions,
    }

    return task_definition


@dataclass
class CurrentEcsTaskMetadata:
    cluster: str
    task_arn: str


def current_ecs_task_metadata() -> CurrentEcsTaskMetadata:
    task_metadata_uri = _container_metadata_uri() + "/task"
    response = requests.get(task_metadata_uri).json()
    cluster = response.get("Cluster")
    task_arn = response.get("TaskARN")

    return CurrentEcsTaskMetadata(cluster=cluster, task_arn=task_arn)


def _container_metadata_uri():
    """
    ECS injects an environment variable into each Fargate task. The value
    of this environment variable is a url that can be queried to introspect
    information about the current processes's running task:

    https://docs.aws.amazon.com/AmazonECS/latest/userguide/task-metadata-endpoint-v4-fargate.html
    """
    return os.environ.get("ECS_CONTAINER_METADATA_URI_V4")


def current_ecs_container_name():
    return requests.get(_container_metadata_uri()).json()["Name"]


def current_ecs_task(ecs, task_arn, cluster):
    def describe_task_or_raise(task_arn, cluster):
        try:
            return ecs.describe_tasks(tasks=[task_arn], cluster=cluster,)[
                "tasks"
            ][0]
        except IndexError:
            raise EcsNoTasksFound

    try:
        task = backoff(
            describe_task_or_raise,
            retry_on=(EcsNoTasksFound,),
            kwargs={"task_arn": task_arn, "cluster": cluster},
            max_retries=BACKOFF_RETRIES,
        )
    except EcsNoTasksFound:
        raise EcsEventualConsistencyTimeout

    return task


def current_ecs_task_config(
    ec2,
    cluster,
    task,
    task_definition,
    environment,
    secrets,
    container_name,
    image,
):
    enis = []
    subnets = []
    for attachment in task["attachments"]:
        if attachment["type"] == "ElasticNetworkInterface":
            for detail in attachment["details"]:
                if detail["name"] == "subnetId":
                    subnets.append(detail["value"])
                if detail["name"] == "networkInterfaceId":
                    enis.append(ec2.NetworkInterface(detail["value"]))

    public_ip = False
    security_groups = []
    for eni in enis:
        if (eni.association_attribute or {}).get("PublicIp"):
            public_ip = True
        for group in eni.groups:
            security_groups.append(group["GroupId"])

    execution_role_arn = task_definition.get("executionRoleArn")
    task_role_arn = task_definition.get("taskRoleArn")

    return DagsterEcsTaskConfig(
        container_name=container_name,
        image=image,
        family=task_definition["family"],
        cluster=cluster,
        subnets=subnets,
        security_groups=security_groups,
        execution_role_arn=execution_role_arn,
        task_role_arn=task_role_arn,
        assign_public_ip="ENABLED" if public_ip else "DISABLED",
        environment=environment,
        secrets=secrets,
    )
