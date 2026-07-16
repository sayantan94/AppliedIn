#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { AppliedInStack } from "../lib/appliedin-stack";

const app = new cdk.App();

new AppliedInStack(app, "AppliedInStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? "us-east-1",
  },
  description: "AppliedIn — autonomous job-application pipeline (single-stack).",
});
