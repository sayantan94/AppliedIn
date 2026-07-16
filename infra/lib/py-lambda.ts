import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";

const REPO_ROOT = path.join(__dirname, "..", "..");

export interface PyLambdaProps {
  /** workspace package dir name under packages/, e.g. "discovery". */
  pkg: string;
  /** dotted handler path, e.g. "appliedin_discovery.handler.handler". */
  handler: string;
  environment?: Record<string, string>;
  timeout?: Duration;
  memoryMiB?: number;
}

/**
 * A Python 3.12 zip Lambda bundled from the monorepo: pip-installs core + the
 * named service package into the asset, and copies the repo-versioned config/
 * so watchlist/preferences travel with the deploy. Bundling runs in the CDK
 * Python build image (requires Docker at synth/deploy time).
 */
export function pyLambda(scope: Construct, id: string, props: PyLambdaProps): lambda.Function {
  return new lambda.Function(scope, id, {
    runtime: lambda.Runtime.PYTHON_3_12,
    handler: props.handler,
    timeout: props.timeout ?? Duration.minutes(5),
    memorySize: props.memoryMiB ?? 512,
    logRetention: logs.RetentionDays.ONE_MONTH,
    environment: { APPLIEDIN_CONFIG_DIR: "config", ...props.environment },
    code: lambda.Code.fromAsset(REPO_ROOT, {
      bundling: {
        image: lambda.Runtime.PYTHON_3_12.bundlingImage,
        command: [
          "bash",
          "-c",
          [
            "pip install --no-cache-dir ./packages/core -t /asset-output",
            `pip install --no-cache-dir ./packages/${props.pkg} -t /asset-output`,
            "cp -r config /asset-output/config",
          ].join(" && "),
        ],
      },
    }),
  });
}
