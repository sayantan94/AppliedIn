import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as sqs from "aws-cdk-lib/aws-sqs";

/**
 * The two pipeline queues plus the internal WhatsApp-processing queue, each
 * with a dead-letter queue. maxReceiveCount = 2 matches the "max 2 attempts
 * per job" guardrail — a message that fails twice lands in its DLQ instead of
 * hammering a portal.
 */
export class Queues extends Construct {
  readonly tailorQueue: sqs.Queue;
  readonly applyQueue: sqs.Queue;
  readonly whatsappQueue: sqs.Queue;
  readonly applyDlq: sqs.Queue;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    this.tailorQueue = this.withDlq("Tailor", Duration.minutes(15)).queue;
    // Apply messages can wait behind the concurrency lid; give them a long
    // visibility timeout so a slow Fargate task doesn't cause redelivery.
    const apply = this.withDlq("Apply", Duration.minutes(30));
    this.applyQueue = apply.queue;
    this.applyDlq = apply.dlq;
    this.whatsappQueue = this.withDlq("WhatsApp", Duration.minutes(2)).queue;
  }

  private withDlq(name: string, visibility: Duration): { queue: sqs.Queue; dlq: sqs.Queue } {
    const dlq = new sqs.Queue(this, `${name}Dlq`, {
      retentionPeriod: Duration.days(14),
    });
    const queue = new sqs.Queue(this, `${name}Queue`, {
      visibilityTimeout: visibility,
      deadLetterQueue: { queue: dlq, maxReceiveCount: 2 },
    });
    return { queue, dlq };
  }
}
