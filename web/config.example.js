// Shape of the runtime config CDK writes into the S3 bucket at deploy time.
// (The committed config.js ships demo mode; this documents the live shape.)
window.APPLIEDIN_CONFIG = {
  demo: false,
  env: "live",
  apiUrl: "https://<api-id>.execute-api.<region>.amazonaws.com",
  cognitoDomain: "https://<domain-prefix>.auth.<region>.amazoncognito.com",
  clientId: "<cognito-app-client-id>",
  redirectUri: "https://<cloudfront-domain>/",
};
