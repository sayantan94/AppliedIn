import { Construct } from "constructs";
import { Duration } from "aws-cdk-lib";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as path from "path";

const REPO_ROOT = path.join(__dirname, "..", "..");

export interface PyLambdaProps {
  /** dotted handler path, e.g. "discovery.handler.handler". */
  handler: string;
  environment?: Record<string, string>;
  timeout?: Duration;
  memoryMiB?: number;
}

/**
 * A Python 3.12 zip Lambda bundled from the repo: pip-installs the single
 * project (all src/ components — core, discovery, …) into the asset, and copies
 * the repo-versioned config/ so watchlist/preferences travel with the deploy.
 * Bundling runs in the CDK Python build image (requires Docker at synth/deploy).
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
      // Bundle only project sources; never sweep in build/output trees (which
      // include cdk.out itself — that recurses).
      exclude: [
        "cdk.out",
        "node_modules",
        ".venv",
        ".git",
        "infra",
        "web",
        "hld",
        "docs",
        "**/__pycache__",
        "**/*.pyc",
      ],
      bundling: {
        image: lambda.Runtime.PYTHON_3_12.bundlingImage,
        command: [
          "bash",
          "-c",
          [
            "pip install --no-cache-dir . -t /asset-output",
            "cp -r config /asset-output/config",
          ].join(" && "),
        ],
      },
    }),
  });
}
