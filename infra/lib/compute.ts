import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecr_assets from "aws-cdk-lib/aws-ecr-assets";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";
import { DataResources } from "./data";
import { Queues } from "./queues";

/**
 * The Fargate compute that runs the apply worker and the career-site crawler.
 *
 * IP-ROTATION CORE: the VPC has PUBLIC subnets only and natGateways: 0. Each
 * job runs as its own task launched with assignPublicIp=ENABLED, so every
 * application gets a fresh public IP that is torn down afterward. A NAT gateway
 * would funnel all tasks through one static EIP and defeat the rotation — so
 * there deliberately is none.
 */
export class Compute extends Construct {
  readonly vpc: ec2.Vpc;
  readonly cluster: ecs.Cluster;
  readonly taskDefinition: ecs.FargateTaskDefinition;
  readonly containerName = "worker";
  readonly securityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, data: DataResources, queues: Queues) {
    super(scope, id);

    this.vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs: 2,
      natGateways: 0, // <-- no NAT: tasks use their own public IPs (IP rotation)
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    this.securityGroup = new ec2.SecurityGroup(this, "WorkerSg", {
      vpc: this.vpc,
      description: "AppliedIn Fargate worker — egress only",
      allowAllOutbound: true,
    });

    this.cluster = new ecs.Cluster(this, "Cluster", { vpc: this.vpc });

    const image = new ecr_assets.DockerImageAsset(this, "WorkerImage", {
      directory: path.join(__dirname, "..", ".."),
      file: path.join("packages", "worker", "Dockerfile"),
    });

    this.taskDefinition = new ecs.FargateTaskDefinition(this, "WorkerTask", {
      cpu: 2048,
      memoryLimitMiB: 4096,
    });

    this.taskDefinition.addContainer(this.containerName, {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "worker",
        logRetention: logs.RetentionDays.ONE_MONTH,
      }),
      environment: {
        APPLIEDIN_APPLICATIONS_TABLE: data.applications.tableName,
        APPLIEDIN_ANSWER_BANK_TABLE: data.answerBank.tableName,
        APPLIEDIN_ARTIFACTS_BUCKET: data.artifacts.bucketName,
        APPLIEDIN_APPLY_QUEUE_URL: queues.applyQueue.queueUrl,
        APPLIEDIN_TAILOR_QUEUE_URL: queues.tailorQueue.queueUrl,
      },
      stopTimeout: Duration.seconds(120),
    });

    // Task role grants — the worker reads/writes tracking + answers, artifacts,
    // portal/platform secrets, and enqueues follow-up messages.
    data.applications.grantReadWriteData(this.taskDefinition.taskRole);
    data.answerBank.grantReadWriteData(this.taskDefinition.taskRole);
    data.artifacts.grantReadWrite(this.taskDefinition.taskRole);
    data.whatsappSecret.grantRead(this.taskDefinition.taskRole);
    data.gmailSecret.grantRead(this.taskDefinition.taskRole);
    queues.applyQueue.grantSendMessages(this.taskDefinition.taskRole);
    queues.tailorQueue.grantSendMessages(this.taskDefinition.taskRole);
  }
}
