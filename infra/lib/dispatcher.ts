import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import { Compute } from "./compute";
import { Queues } from "./queues";
import { pyLambda } from "./py-lambda";

/**
 * Dispatcher: drains the apply queue and launches ONE Fargate task per job
 * (assignPublicIp=ENABLED → fresh IP per application). Enforces a concurrency
 * lid; over the lid it lets the message redeliver.
 */
export class Dispatcher extends Construct {
  constructor(scope: Construct, id: string, compute: Compute, queues: Queues) {
    super(scope, id);

    const publicSubnetIds = compute.vpc.publicSubnets.map((s) => s.subnetId).join(",");

    const fn = pyLambda(this, "Fn", {
      pkg: "dispatcher",
      handler: "appliedin_dispatcher.handler.handler",
      timeout: Duration.minutes(1),
      environment: {
        APPLIEDIN_ECS_CLUSTER: compute.cluster.clusterName,
        APPLIEDIN_TASK_DEFINITION: compute.taskDefinition.taskDefinitionArn,
        APPLIEDIN_SUBNETS: publicSubnetIds,
        APPLIEDIN_SECURITY_GROUP: compute.securityGroup.securityGroupId,
        APPLIEDIN_WORKER_CONTAINER: compute.containerName,
        APPLIEDIN_MAX_CONCURRENT: "3",
      },
    });

    fn.addEventSource(new SqsEventSource(queues.applyQueue, { batchSize: 1 }));

    // RunTask on the worker task def + list/describe to count running tasks.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:RunTask"],
        resources: [compute.taskDefinition.taskDefinitionArn],
      }),
    );
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["ecs:ListTasks", "ecs:DescribeTasks"],
        resources: ["*"],
        conditions: {
          ArnEquals: { "ecs:cluster": compute.cluster.clusterArn },
        },
      }),
    );
    // PassRole for the task's execution + task roles (both are handed to ECS
    // when the task launches).
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [
          compute.taskDefinition.taskRole.roleArn,
          compute.taskDefinition.obtainExecutionRole().roleArn,
        ],
      }),
    );
  }
}
