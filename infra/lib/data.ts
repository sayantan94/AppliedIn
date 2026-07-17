import { Construct } from "constructs";
import { RemovalPolicy } from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";

/**
 * Stateful resources: the two DynamoDB tables (tracking + answer bank),
 * the artifact bucket, and the platform secrets. Kept in its own construct
 * so the data schema is defined in one place and matches the Python stores.
 */
export class DataResources extends Construct {
  readonly applications: dynamodb.Table;
  readonly answerBank: dynamodb.Table;
  readonly artifacts: s3.Bucket;
  readonly whatsappSecret: secretsmanager.Secret;
  readonly gmailSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // applications: pk = company#job_id. GSIs mirror core.storage.tracking.
    this.applications = new dynamodb.Table(this, "Applications", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });
    this.applications.addGlobalSecondaryIndex({
      indexName: "jd_hash-index",
      partitionKey: { name: "jd_hash", type: dynamodb.AttributeType.STRING },
    });
    this.applications.addGlobalSecondaryIndex({
      indexName: "status-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
    });

    // answer_bank: pk = "global" | "company#<name>", sk = normalized question label.
    this.answerBank = new dynamodb.Table(this, "AnswerBank", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: RemovalPolicy.RETAIN,
    });

    this.artifacts = new s3.Bucket(this, "Artifacts", {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
    });

    // Portal creds are per-company secrets (portal/<name>) created out of band
    // or by auto-signup; these two are the platform-level secrets.
    this.whatsappSecret = new secretsmanager.Secret(this, "WhatsAppSecret", {
      secretName: "appliedin/whatsapp",
      description: "Meta token, phone_number_id, app_secret, verify_token, owner wa_id",
    });
    this.gmailSecret = new secretsmanager.Secret(this, "GmailSecret", {
      secretName: "appliedin/gmail",
      description: "Gmail OAuth refresh token (readonly scope) for signup verification",
    });
  }
}
