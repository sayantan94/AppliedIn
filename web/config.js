// Runtime config. In deploys, CDK overwrites this file in the S3 bucket with
// the real Cognito + API values. The committed default runs in demo mode so
// the dashboard renders locally with sample data (open index.html or serve
// this directory, no backend required).
window.APPLIEDIN_CONFIG = {
  demo: true,

  // --- filled by CDK on deploy (see config.example.js) ---
  // env: "live",
  // apiUrl: "https://xxxx.execute-api.us-east-1.amazonaws.com",
  // cognitoDomain: "https://appliedin.auth.us-east-1.amazoncognito.com",
  // clientId: "xxxxxxxxxxxxxxxxxxxx",
  // redirectUri: "https://dxxxx.cloudfront.net/",
};
