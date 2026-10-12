"""Component that runs the Pulumi customer managed workflow agent in Kubernetes."""

from typing import Any

import pulumi
import pulumi_kubernetes as k8s


class WorkflowAgent(pulumi.ComponentResource):
    """Runs the Pulumi customer managed workflow agent in Kubernetes.

    The agents poll Pulumi Cloud for work and launch a runner pod in the same namespace for
    each job.
    """

    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service

    def __init__(
        self,
        name: str,
        namespace: pulumi.Input[str],
        image: pulumi.Input[str],
        image_pull_policy: pulumi.Input[str],
        replicas: pulumi.Input[int],
        agent_pool_token: pulumi.Input[str],
        service_url: pulumi.Input[str],
        runner_cpus: pulumi.Input[float] | None = None,
        runner_memory_gb: pulumi.Input[float] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Create the agent.

        Args:
            name: Component name, used as the prefix of the child resources.
            namespace: Namespace the agents and the runner pods they launch live in.
            image: Agent image, which also supplies the init container of every runner pod.
            image_pull_policy: Kubernetes image pull policy for the agent and runner pods.
            replicas: Number of agent replicas.
            agent_pool_token: Access token of the Pulumi agent pool the agents register with.
            service_url: Pulumi Cloud API URL.
            runner_cpus: CPUs requested by each runner pod. The agent default is used when unset.
            runner_memory_gb: Memory in GB requested by each runner pod. The agent default is
                about 4 GB, which does not fit on small nodes, so size this to the node type.
            opts: Standard resource options.
        """
        super().__init__("pequod:k8s:WorkflowAgent", name, {}, opts)

        labels = {"app.kubernetes.io/name": "customer-managed-workflow-agent"}
        child = pulumi.ResourceOptions(parent=self)

        agent_config = k8s.core.v1.ConfigMap(
            f"{name}-config",
            metadata={"namespace": namespace, "labels": labels},
            data={
                "PULUMI_AGENT_SERVICE_URL": service_url,
                "PULUMI_AGENT_IMAGE": image,
                "PULUMI_AGENT_IMAGE_PULL_POLICY": image_pull_policy,
            },
            opts=child,
        )

        agent_secret = k8s.core.v1.Secret(
            f"{name}-token",
            metadata={"namespace": namespace, "labels": labels},
            string_data={"PULUMI_AGENT_TOKEN": agent_pool_token},
            opts=child,
        )

        # Identity for the agent itself; it needs to manage runner pods.
        agent_service_account = k8s.core.v1.ServiceAccount(
            f"{name}-agent",
            metadata={"namespace": namespace, "labels": labels},
            opts=child,
        )

        # Identity for the runner pods. Attach cloud credentials (e.g. IRSA) here if
        # deployments in this pool need them.
        worker_service_account = k8s.core.v1.ServiceAccount(
            f"{name}-worker",
            metadata={"namespace": namespace, "labels": labels},
            opts=child,
        )

        role = k8s.rbac.v1.Role(
            f"{name}-agent",
            metadata={"namespace": namespace, "labels": labels},
            rules=[
                {
                    "api_groups": [""],
                    "resources": ["pods", "pods/log", "configmaps"],
                    "verbs": ["create", "get", "list", "watch", "update", "delete"],
                }
            ],
            opts=child,
        )

        k8s.rbac.v1.RoleBinding(
            f"{name}-agent",
            metadata={"namespace": namespace, "labels": labels},
            subjects=[
                {
                    "kind": "ServiceAccount",
                    "name": agent_service_account.metadata.name,
                    "namespace": namespace,
                }
            ],
            role_ref={
                "kind": "Role",
                "name": role.metadata.name,
                "api_group": "rbac.authorization.k8s.io",
            },
            opts=child,
        )

        def config_ref(key: str) -> dict[str, Any]:
            return {
                "config_map_key_ref": {"name": agent_config.metadata.name, "key": key}
            }

        env: list[Any] = [
            {"name": "PULUMI_AGENT_DEPLOY_TARGET", "value": "kubernetes"},
            {"name": "PULUMI_AGENT_SHARED_VOLUME_DIRECTORY", "value": "/mnt/work"},
            {
                "name": "PULUMI_AGENT_SERVICE_URL",
                "value_from": config_ref("PULUMI_AGENT_SERVICE_URL"),
            },
            {
                "name": "PULUMI_AGENT_IMAGE",
                "value_from": config_ref("PULUMI_AGENT_IMAGE"),
            },
            {
                "name": "PULUMI_AGENT_IMAGE_PULL_POLICY",
                "value_from": config_ref("PULUMI_AGENT_IMAGE_PULL_POLICY"),
            },
            {
                "name": "PULUMI_AGENT_TOKEN",
                "value_from": {
                    "secret_key_ref": {
                        "name": agent_secret.metadata.name,
                        "key": "PULUMI_AGENT_TOKEN",
                    }
                },
            },
            {
                "name": "PULUMI_AGENT_SERVICE_ACCOUNT_NAME",
                "value": worker_service_account.metadata.name,
            },
        ]
        if runner_cpus is not None:
            env.append(
                {
                    "name": "PULUMI_AGENT_NUM_CPUS",
                    "value": pulumi.Output.from_input(runner_cpus).apply(_as_string),
                }
            )
        if runner_memory_gb is not None:
            env.append(
                {
                    "name": "PULUMI_AGENT_MEM_QUANTITY",
                    "value": pulumi.Output.from_input(runner_memory_gb).apply(
                        _as_string
                    ),
                }
            )

        deployment_spec: k8s.apps.v1.DeploymentSpecArgsDict = {
            "replicas": replicas,
            "selector": {"match_labels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "service_account_name": agent_service_account.metadata.name,
                    "containers": [
                        {
                            "name": "agent",
                            "image": image,
                            "image_pull_policy": image_pull_policy,
                            "env": env,
                            "ports": [
                                {
                                    "name": "http",
                                    "container_port": 8080,
                                    "protocol": "TCP",
                                }
                            ],
                            "volume_mounts": [
                                {"name": "agent-work", "mount_path": "/mnt/work"}
                            ],
                        }
                    ],
                    "volumes": [{"name": "agent-work", "empty_dir": {}}],
                },
            },
        }
        self.deployment = k8s.apps.v1.Deployment(
            f"{name}-pool",
            metadata={"namespace": namespace, "labels": labels},
            spec=deployment_spec,
            opts=child,
        )

        # Exposes the agent health endpoint (/healthz) inside the cluster.
        self.service = k8s.core.v1.Service(
            f"{name}-health",
            metadata={
                "namespace": namespace,
                "labels": {**labels, "app.kubernetes.io/component": "metrics"},
                "annotations": {
                    "prometheus.io/scrape": "true",
                    "prometheus.io/port": "8080",
                    "prometheus.io/path": "/healthz",
                },
            },
            spec={
                "selector": labels,
                "ports": [
                    {
                        "name": "http",
                        "port": 8080,
                        "target_port": 8080,
                        "protocol": "TCP",
                    }
                ],
                "type": "ClusterIP",
            },
            opts=child,
        )

        self.register_outputs({"deployment": self.deployment, "service": self.service})


def _as_string(value: Any) -> str:
    """Render a number the way the agent expects it (1.0 -> "1", 2 -> "2")."""
    return str(int(value)) if float(value).is_integer() else str(value)
