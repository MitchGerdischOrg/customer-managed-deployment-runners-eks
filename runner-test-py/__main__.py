"""Random pet name deployed on the EKS customer managed agent pool (Python)."""

import pulumi
import pulumi_pulumiservice as pulumiservice
import pulumi_random as random

config = pulumi.Config()
pet_length = config.get_int("petLength") or 7

# Random pet name from the random provider. This uses the provider directly instead of the
# private random-abstracted component: that component is written in Node.js, so installing it
# needs the Node language plugin, which the small Python-only executor image does not include.
pet = random.RandomPet("petName", length=pet_length)

pulumi.export("petName", pet.id)

# --- Deployment settings -------------------------------------------------------------------
# Captures how this stack's Pulumi Deployments are configured, so the setup is reviewable and
# reproducible instead of living only in Pulumi Cloud.

org = pulumi.get_organization()
deploy_on_push = config.get_bool("deployOnPush") is not False

# The customer managed agent pool and its executor image are owned by the
# eks-deployment-agent-pool-py stack, so read them from its outputs rather than hard-coding ids.
pool_stack = pulumi.StackReference(
    config.get("poolStack") or f"{org}/eks-deployment-agent-pool-py/dev"
)

pulumiservice.DeploymentSettings(
    "deployment-settings",
    organization=org,
    project=pulumi.get_project(),
    stack=pulumi.get_stack(),
    # Run deployments on the agents in the shared EKS cluster instead of Pulumi-hosted runners.
    agent_pool_id=pool_stack.require_output("poolId").apply(str),
    # Python executor image mirrored into the account's private ECR. Pulled by the cluster
    # nodes via their IAM role, so no registry credentials are needed.
    executor_context={
        "executor_image": pool_stack.require_output("executorImageUri").apply(str)
    },
    # Where the program is deployed from. Updates triggered in the console use this branch.
    source_context={"git": {"branch": config.get("deployBranch") or "main"}},
    vcs={
        "provider": "github",
        "repository": config.require("repository")
        # Run `pulumi up` when a commit is pushed to the deploy branch. Turn off with
        # `pulumi config set deployOnPush false` to deploy manually instead.
        "deploy_commits": deploy_on_push,
        "preview_pull_requests": False,
        "pull_request_template": False,
    },
    # Dependency caching is intentionally not enabled: Pulumi Cloud rejects it for jobs that
    # run on customer managed agents.
    opts=pulumi.ResourceOptions(
        # The 24h TTL runs a destroy using these settings. Keep them so the stack can still be
        # redeployed afterwards and the destroy does not delete the settings it is running with.
        retain_on_delete=True,
    ),
)
