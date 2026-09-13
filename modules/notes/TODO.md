# notes — open work

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Open work below.

- [ ] **stream → EB fan-out** — table has streams enabled but no pipe. Wire `notes.added/updated` events when a downstream consumer needs them
- [ ] **Cedar policy layer** — e.g. retention rules, who-can-delete. Phase 5
- [ ] **version TTL / compaction** — old versions accumulate; add a TTL or archival sweep if storage becomes a concern (not at current volume)

## tf→cf migration spike — proof (2026-05-26)

Hand-port of this module to native CloudFormation, to test the `prod/tower/TODO.md` "substrate" migration claim against a representative module (DDB + 4 GSIs + stream, the 5-lambda fan-out, agent gateway-target registration, cross-module inputs). **Illustrative** — not `cfn-lint`'d, and `GatewayTarget.TargetConfiguration`'s deep shape is sketched (`ToolSchema` ported verbatim from `schema.json`); pin both before any real port.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::LanguageExtensions          # enables Fn::ForEach (the for_each fan-out)

Parameters:
  CustomerId:        { Type: String }
  StackPrefix:       { Type: String, Default: gerp }
  SchemaTableName:   { Type: String }                          # schemas-stack output (or Fn::ImportValue)
  CodeBucket:        { Type: String }                          # lambda-zip staging bucket
  CodeKeyPrefix:     { Type: String }
  RegisterWithAgent: { Type: String, AllowedValues: ['true','false'], Default: 'true' }
  GatewayId:         { Type: 'AWS::SSM::Parameter::Value<String>' }   # resolves /…/agent/gateway_id at deploy
  GatewayRoleArn:    { Type: 'AWS::SSM::Parameter::Value<String>' }   # ← direct analog of TF data.aws_ssm_parameter

Conditions:
  DoRegister: !Equals [!Ref RegisterWithAgent, 'true']

Resources:
  NotesTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: !Sub '${StackPrefix}-notes-${CustomerId}'
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - { AttributeName: note_id,           AttributeType: S }
        - { AttributeName: version_ts,        AttributeType: S }
        - { AttributeName: contact_id,        AttributeType: S }
        - { AttributeName: journal_entry_id,  AttributeType: S }
        - { AttributeName: purchase_order_id, AttributeType: S }
        - { AttributeName: invoice_id,        AttributeType: S }
      KeySchema:
        - { AttributeName: note_id,    KeyType: HASH }
        - { AttributeName: version_ts, KeyType: RANGE }
      GlobalSecondaryIndexes:
        - IndexName: contact-index
          KeySchema: [{AttributeName: contact_id, KeyType: HASH}, {AttributeName: version_ts, KeyType: RANGE}]
          Projection: { ProjectionType: ALL }
        - IndexName: journal-entry-index
          KeySchema: [{AttributeName: journal_entry_id, KeyType: HASH}, {AttributeName: version_ts, KeyType: RANGE}]
          Projection: { ProjectionType: ALL }
        # + purchase-order-index, invoice-index (same shape)
      StreamSpecification: { StreamViewType: NEW_AND_OLD_IMAGES }   # consumer wiring is the open item above

  LambdaRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${StackPrefix}-notes-${CustomerId}-lambda'
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement: [{ Effect: Allow, Principal: {Service: lambda.amazonaws.com}, Action: sts:AssumeRole }]
      Policies:
        - PolicyName: notes
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action: [dynamodb:GetItem, dynamodb:PutItem, dynamodb:UpdateItem, dynamodb:Query, dynamodb:Scan]
                Resource: [!GetAtt NotesTable.Arn, !Sub '${NotesTable.Arn}/index/*']
              - { Effect: Allow, Action: dynamodb:Query, Resource: !Sub 'arn:aws:dynamodb:${AWS::Region}:${AWS::AccountId}:table/${SchemaTableName}' }
              - { Effect: Allow, Action: [logs:CreateLogGroup, logs:CreateLogStream, logs:PutLogEvents], Resource: !Sub 'arn:aws:logs:${AWS::Region}:${AWS::AccountId}:*' }

  'Fn::ForEach::Fns':                          # the notes lambda
    - Fn
    - [manage_notes]
    - 'NotesFn&{Fn}':
        Type: AWS::Lambda::Function
        Properties:
          FunctionName: !Sub '${StackPrefix}-notes-${CustomerId}-${Fn}'
          Runtime: python3.12
          Handler: main.handler
          Role: !GetAtt LambdaRole.Arn
          Timeout: 30
          Code: { S3Bucket: !Ref CodeBucket, S3Key: !Sub '${CodeKeyPrefix}/${Fn}.zip' }
          Environment:
            Variables: { NOTES_TABLE: !Ref NotesTable, SCHEMA_TABLE: !Ref SchemaTableName, CUSTOMER_ID: !Ref CustomerId }

  'Fn::ForEach::Targets':                      # gateway target + invoke perm per fn, gated on registration
    - Fn
    - [manage_notes]
    - 'NotesTarget&{Fn}':
        Type: AWS::BedrockAgentCore::GatewayTarget
        Condition: DoRegister
        Properties:
          Name: !Sub 'notes-${Fn}'             # NOTE: Name pattern forbids '_' — needs a pre-hyphenated list
          GatewayIdentifier: !Ref GatewayId    # Update requires: Replacement (same ForceNew churn as TF)
          Description: !Sub 'notes ${Fn}'      # ≤200 chars (same cap we hit in TF)
          CredentialProviderConfigurations: [{ CredentialProviderType: GATEWAY_IAM_ROLE }]
          TargetConfiguration:
            Mcp:
              Lambda:
                LambdaArn: !GetAtt 'NotesFn&{Fn}.Arn'
                ToolSchema: { InlinePayload: [ '...' ] }   # ← ported verbatim from lambdas/<fn>/schema.json
      'NotesPerm&{Fn}':
        Type: AWS::Lambda::Permission
        Condition: DoRegister
        Properties:
          FunctionName: !Ref 'NotesFn&{Fn}'
          Action: lambda:InvokeFunction
          Principal: !Ref GatewayRoleArn

Outputs:
  NotesTableName: { Value: !Ref NotesTable, Export: { Name: !Sub '${StackPrefix}-notes-${CustomerId}-table' } }
  StreamArn:      { Value: !GetAtt NotesTable.StreamArn }
```

### mapping
| terraform | cloudformation | clean? |
|---|---|---|
| `aws_dynamodb_table` (4 GSI + stream) | `AWS::DynamoDB::Table` | mechanical |
| `aws_iam_role` + `aws_iam_role_policy` | `AWS::IAM::Role` (inline `Policies`) | mechanical |
| `aws_lambda_function` ×5 (`for_each`) | `AWS::Lambda::Function` ×5 via `Fn::ForEach` | mechanical (needs `LanguageExtensions`) |
| `archive_file` (local zip) | `Code: {S3Bucket,S3Key}` | **needs S3 staging** |
| `aws_bedrockagentcore_gateway_target` ×5 | `AWS::BedrockAgentCore::GatewayTarget` ×5 | native; `ToolSchema` verbose |
| `aws_lambda_permission` ×5 | `AWS::Lambda::Permission` ×5 | mechanical |
| `data.aws_ssm_parameter` (gateway id/role) | `AWS::SSM::Parameter::Value<String>` param | mechanical (deploy-time resolve) |
| `var.schema_table_name` | Parameter (or `Fn::ImportValue`) | mechanical |
| `count = register_with_agent` | `Condition` + `Condition:` on resources | mechanical |
| module outputs | `Outputs` (+ `Export` for cross-stack) | mechanical |

### friction (none a blocker)
1. **lambda code → S3.** TF builds zips locally (`archive_file`); CFN's `Code` points at S3, so a build step must stage zips to `CodeBucket`. CDK automates this via assets. The one real operational delta.
2. **inline `ToolSchema` favors CDK/codegen.** TF reads `schema.json` + `dynamic` blocks to generate the nested MCP tool schema. Raw CFN can't read a file + generate nesting → inline each schema literally (un-DRY) or emit the template from `schema.json` via CDK/codegen. The one spot raw CFN is worse than TF.
3. **no string fns.** CFN has no `replace()`; `GatewayTarget.Name` forbids `_`, so pre-hyphenate the loop list (and `CustomerId` in resource names). Minor — pre-shape inputs.

### verdict
Clean native port. Every resource has a native type — including the AgentCore gateway targets. Cross-module inputs map to Parameters + SSM-typed Parameters (the direct analog of `data.aws_ssm_parameter`); `register_with_agent` → a `Condition`. The TF gotchas transfer 1:1 (`Description` ≤200; `GatewayIdentifier` = `Replacement`/ForceNew). Confirms the "mechanical port" claim for a representative module. **CDK vs raw CFN turns on friction #2** — CDK reading `schema.json` to emit the targets is materially nicer than hand-inlining schemas, so a real port should evaluate CDK-synth-to-CFN, not just hand-written templates.
