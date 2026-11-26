# Credentials and node types

Credential values are secret data. Never return, log or store them in node parameters or
source code. Validate the exact credential type before any external call.

## Langfuse

`langfuse_evaluation` uses the team's Langfuse credential: type `langfuseApi`, with the values
`baseUrl`, `publicKey` and `secretKey`. Studio creates it under Team settings > Langfuse, with the
code `langfuse_api`; that type cannot be created from the credentials screen.

- Selection: `ctx.credentials.resolve(code, mode="team", expected_type="langfuseApi")`. An empty
  selection on the node means the code `langfuse_api`. There is no personal or caller variant.
- Use: the two keys form the Basic auth header of each request to `baseUrl`. They never appear in
  output, trace events or error messages; the public key is also sent as the scope attribute and
  header Langfuse expects.
- Lifecycle: static keys, no refresh. Rotating the keys in the team settings is picked up by the
  next run. A disabled Langfuse setting makes the credential inactive, and the node then reports
  that the team has no active Langfuse credential.

`juridische_adviseur_fase_a` is a pure data transformation and uses no credentials.
