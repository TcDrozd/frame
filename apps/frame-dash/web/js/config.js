// Stack outputs from `sam deploy` (CloudFormation stack "frame-dash").
// Update these after a fresh deploy: aws cloudformation describe-stacks
// --stack-name frame-dash --query 'Stacks[0].Outputs'
export const CONFIG = {
  apiUrl: "https://c7a3s6xa25.execute-api.us-east-1.amazonaws.com",
  cognitoDomain: "https://frame-dash-tcd.auth.us-east-1.amazoncognito.com",
  clientId: "iim2fupur0j4vmiko4cuf214a",
  manifestUrl: "https://trevor-shared-photo-stream.s3.us-east-1.amazonaws.com/manifest.dev.json",
};
