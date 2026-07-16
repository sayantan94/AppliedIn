import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as iam from "aws-cdk-lib/aws-iam";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import * as path from "path";
import { DataResources } from "./data";
import { Queues } from "./queues";

const REPO_ROOT = path.join(__dirname, "..", "..");

/**
 * Tailoring: a container-image Lambda (Typst bundled) triggered by the tailor
 * queue. Runs the Strands match-score + tailoring agent, the truthfulness
 * validator, renders the PDF, and enqueues survivors to the apply queue.
 */
export class Tailoring extends Construct {
  constructor(scope: Construct, id: string, data: DataResources, queues: Queues) {
    super(scope, id);

    const fn = new lambda.DockerImageFunction(this, "Fn", {
      code: lambda.DockerImageCode.fromImageAsset(REPO_ROOT, {
        file: path.join("infra", "docker", "tailoring.Dockerfile"),
      }),
      timeout: Duration.minutes(10),
      memorySize: 2048,
      logRetention: logs.RetentionDays.ONE_MONTH,
      environment: {
        APPLIEDIN_APPLICATIONS_TABLE: data.applications.tableName,
        APPLIEDIN_ANSWER_BANK_TABLE: data.answerBank.tableName,
        APPLIEDIN_ARTIFACTS_BUCKET: data.artifacts.bucketName,
        APPLIEDIN_APPLY_QUEUE_URL: queues.applyQueue.queueUrl,
      },
    });

    fn.addEventSource(new SqsEventSource(queues.tailorQueue, { batchSize: 1 }));

    data.applications.grantReadWriteData(fn);
    data.answerBank.grantReadData(fn);
    data.artifacts.grantReadWrite(fn);
    queues.applyQueue.grantSendMessages(fn);
    // Bedrock invoke for the tailoring/scoring agents.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: ["*"],
      }),
    );
  }
}
