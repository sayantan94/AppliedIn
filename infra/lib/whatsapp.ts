import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as apigwv2 from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import { DataResources } from "./data";
import { Queues } from "./queues";
import { pyLambda } from "./py-lambda";

/**
 * WhatsApp control surface: a public HTTP API webhook (fast ACK, signature
 * verify) that enqueues raw updates onto an internal queue, and a processor
 * Lambda that routes commands/buttons/free-text (read-only Q&A agent +
 * conversational gate-answer resume).
 */
export class WhatsApp extends Construct {
  readonly apiUrl: string;

  constructor(scope: Construct, id: string, data: DataResources, queues: Queues) {
    super(scope, id);

    const commonEnv = {
      APPLIEDIN_APPLICATIONS_TABLE: data.applications.tableName,
      APPLIEDIN_ANSWER_BANK_TABLE: data.answerBank.tableName,
      APPLIEDIN_ARTIFACTS_BUCKET: data.artifacts.bucketName,
      APPLIEDIN_APPLY_QUEUE_URL: queues.applyQueue.queueUrl,
      APPLIEDIN_WHATSAPP_QUEUE_URL: queues.whatsappQueue.queueUrl,
      APPLIEDIN_WHATSAPP_SECRET: data.whatsappSecret.secretName,
    };

    // Webhook: verify signature, ACK fast, enqueue for async processing.
    const webhook = pyLambda(this, "Webhook", {
      handler: "whatsapp.webhook.handler",
      timeout: Duration.seconds(10),
      environment: commonEnv,
    });
    data.whatsappSecret.grantRead(webhook);
    queues.whatsappQueue.grantSendMessages(webhook);

    // Processor: the real work, off the webhook's request path.
    const processor = pyLambda(this, "Processor", {
      handler: "whatsapp.processor.handler",
      timeout: Duration.minutes(2),
      memoryMiB: 1024,
      environment: commonEnv,
    });
    processor.addEventSource(new SqsEventSource(queues.whatsappQueue, { batchSize: 1 }));
    data.applications.grantReadWriteData(processor);
    data.answerBank.grantReadWriteData(processor);
    data.artifacts.grantReadWrite(processor);
    data.whatsappSecret.grantRead(processor);
    queues.applyQueue.grantSendMessages(processor);
    processor.addToRolePolicy(
      new iam.PolicyStatement({ actions: ["bedrock:InvokeModel"], resources: ["*"] }),
    );

    const api = new apigwv2.HttpApi(this, "Api", {
      description: "AppliedIn WhatsApp webhook",
    });
    const integration = new HttpLambdaIntegration("WebhookIntegration", webhook);
    api.addRoutes({
      path: "/webhook",
      methods: [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      integration,
    });

    this.apiUrl = api.apiEndpoint;
  }
}
