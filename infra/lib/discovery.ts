import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import { DataResources } from "./data";
import { Queues } from "./queues";
import { pyLambda } from "./py-lambda";

/**
 * Discovery: an EventBridge cron (default every 6h) invoking the feed-poller
 * Lambda. Crawl-mode companies are handled by the Fargate crawler, not here.
 */
export class Discovery extends Construct {
  constructor(scope: Construct, id: string, data: DataResources, queues: Queues) {
    super(scope, id);

    const fn = pyLambda(this, "Fn", {
      pkg: "discovery",
      handler: "appliedin_discovery.handler.handler",
      timeout: Duration.minutes(5),
      environment: {
        APPLIEDIN_APPLICATIONS_TABLE: data.applications.tableName,
        APPLIEDIN_TAILOR_QUEUE_URL: queues.tailorQueue.queueUrl,
        APPLIEDIN_APPLY_QUEUE_URL: queues.applyQueue.queueUrl,
      },
    });

    data.applications.grantReadWriteData(fn);
    queues.tailorQueue.grantSendMessages(fn);
    queues.applyQueue.grantSendMessages(fn);

    new events.Rule(this, "Schedule", {
      schedule: events.Schedule.rate(Duration.hours(6)),
      targets: [new targets.LambdaFunction(fn)],
    });
  }
}
