"""Component that mirrors an upstream container image into a private ECR repository."""

import json

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as k8s

# Pinned crane release used to copy images registry-to-registry.
CRANE_IMAGE = "gcr.io/go-containerregistry/crane:v0.22.1"


class ImageMirror(pulumi.ComponentResource):
    """Creates a private ECR repository that holds a copy of an upstream image.

    The cluster then pulls the image from ECR (using the node role) instead of a public
    registry.

    The copy is done by a one-shot Kubernetes Job running crane inside the cluster, because
    Pulumi deployments have no Docker daemon to build or push images. The Job is only
    recreated when the source image or tag changes.

    Requires a kubernetes provider (passed via ``providers``) and the default aws provider.
    """

    repository: aws.ecr.Repository
    copy_job: k8s.batch.v1.Job
    image_uri: pulumi.Output[str]
    """Fully qualified reference of the mirrored image in ECR (registry/repo:tag)."""

    def __init__(
        self,
        name: str,
        source_image: pulumi.Input[str],
        repository_name: pulumi.Input[str],
        tag: pulumi.Input[str],
        namespace: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Create the mirror.

        Args:
            name: Component name, used as the prefix of the child resources.
            source_image: Image to mirror, e.g. docker.io/pulumi/customer-managed-workflow-agent:v2.2.0.
            repository_name: Name of the private ECR repository to create.
            tag: Tag the mirrored image is published under in ECR.
            namespace: Namespace the copy job runs in.
            opts: Standard resource options.
        """
        super().__init__("pequod:ecr:ImageMirror", name, {}, opts)
        child = pulumi.ResourceOptions(parent=self)

        self.repository = aws.ecr.Repository(
            f"{name}-repo",
            name=repository_name,
            image_scanning_configuration={"scan_on_push": True},
            # Mirrored images are reproducible from upstream, so allow the repo to be
            # deleted with the stack even when it still contains images.
            force_delete=True,
            opts=child,
        )

        self.image_uri = pulumi.Output.concat(self.repository.repository_url, ":", tag)

        # Short-lived ECR credentials for pushing, written as a docker config for crane.
        # The token expires after 12 hours, so it is only refreshed when the copy is re-run
        # (the secret is replaced together with the job, see below).
        registry_host = self.repository.repository_url.apply(
            lambda url: url.split("/")[0]
        )
        auth_token = aws.ecr.get_authorization_token_output(
            registry_id=self.repository.registry_id
        )
        docker_config = pulumi.Output.all(
            registry_host, auth_token.authorization_token
        ).apply(lambda args: json.dumps({"auths": {args[0]: {"auth": args[1]}}}))

        secret_name = f"{name}-ecr-push-auth"
        auth_secret = k8s.core.v1.Secret(
            f"{name}-push-auth",
            metadata={
                "name": secret_name,
                "namespace": namespace,
                # Changing the image or tag changes these annotations, which replaces the
                # secret (and so refreshes the token) at the same time as the job.
                "annotations": {
                    "mirror/source-image": source_image,
                    "mirror/tag": tag,
                },
            },
            string_data={"config.json": docker_config},
            opts=pulumi.ResourceOptions(
                parent=self,
                # Do not rewrite the secret on every update just because the token rotated.
                ignore_changes=["string_data"],
                replace_on_changes=["metadata.annotations"],
                delete_before_replace=True,
            ),
        )

        job_spec: k8s.batch.v1.JobSpecArgsDict = {
            "backoff_limit": 3,
            "active_deadline_seconds": 900,
            "template": {
                "spec": {
                    "restart_policy": "Never",
                    "containers": [
                        {
                            "name": "crane",
                            "image": CRANE_IMAGE,
                            # Copies the full manifest list, so all published platforms are
                            # mirrored.
                            "args": ["copy", source_image, self.image_uri],
                            "env": [{"name": "DOCKER_CONFIG", "value": "/auth"}],
                            "volume_mounts": [
                                {
                                    "name": "auth",
                                    "mount_path": "/auth",
                                    "read_only": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "auth", "secret": {"secret_name": secret_name}}
                    ],
                }
            },
        }
        self.copy_job = k8s.batch.v1.Job(
            f"{name}-copy",
            metadata={"namespace": namespace},
            spec=job_spec,
            opts=pulumi.ResourceOptions(
                parent=self,
                depends_on=[auth_secret, self.repository],
                replace_on_changes=["spec"],
            ),
        )

        self.register_outputs(
            {
                "repository": self.repository,
                "copy_job": self.copy_job,
                "image_uri": self.image_uri,
            }
        )
