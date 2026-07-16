import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import { Queues } from "./queues";

/**
 * Operational guardrails (HLD guardrail 5): alarm on a stalled apply pipeline.
 * ApproximateAgeOfOldestMessage on the apply queue catches a dead dispatcher /
 * broken worker image (jobs piling up unprocessed) within one alarm period.
 */
export class Alarms extends Construct {
  constructor(scope: Construct, id: string, queues: Queues) {
    super(scope, id);

    new cloudwatch.Alarm(this, "ApplyQueueStalled", {
      metric: queues.applyQueue.metricApproximateAgeOfOldestMessage({
        period: Duration.minutes(5),
        statistic: "Maximum",
      }),
      threshold: Duration.hours(1).toSeconds(),
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: "Apply queue backing up — dispatcher or worker likely down.",
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    new cloudwatch.Alarm(this, "ApplyDlqNotEmpty", {
      metric: queues.applyQueue.deadLetterQueue!.queue.metricApproximateNumberOfMessagesVisible({
        period: Duration.minutes(5),
        statistic: "Maximum",
      }),
      threshold: 0,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      alarmDescription: "Apply job exhausted its 2 attempts and hit the DLQ.",
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
