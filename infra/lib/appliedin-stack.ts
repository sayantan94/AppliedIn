import { Construct } from "constructs";
import { CfnOutput, Stack, StackProps } from "aws-cdk-lib";
import { DataResources } from "./data";
import { Queues } from "./queues";
import { Compute } from "./compute";
import { Discovery } from "./discovery";
import { Tailoring } from "./tailoring";
import { Dispatcher } from "./dispatcher";
import { WhatsApp } from "./whatsapp";
import { Alarms } from "./alarms";

/**
 * The single AppliedIn stack. Composed from one construct per subsystem so each
 * piece stays independently readable; wiring (grants, env, event sources) lives
 * inside the constructs, not here.
 */
export class AppliedInStack extends Stack {
  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);

    const data = new DataResources(this, "Data");
    const queues = new Queues(this, "Queues");
    const compute = new Compute(this, "Compute", data, queues);

    new Discovery(this, "Discovery", data, queues);
    new Tailoring(this, "Tailoring", data, queues);
    new Dispatcher(this, "Dispatcher", compute, queues);
    const whatsapp = new WhatsApp(this, "WhatsApp", data, queues);
    new Alarms(this, "Alarms", queues);

    new CfnOutput(this, "WhatsAppWebhookUrl", {
      value: `${whatsapp.apiUrl}/webhook`,
      description: "Set this as the Meta WhatsApp webhook callback URL.",
    });
    new CfnOutput(this, "ApplicationsTableName", { value: data.applications.tableName });
    new CfnOutput(this, "AnswerBankTableName", { value: data.answerBank.tableName });
    new CfnOutput(this, "ArtifactsBucketName", { value: data.artifacts.bucketName });
  }
}
