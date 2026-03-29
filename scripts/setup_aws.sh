#!/usr/bin/env bash
# Creates the AWS resources needed for the Alexa-Claude bridge.
# Prerequisites: AWS CLI configured with credentials (aws configure)
#
# Usage: bash scripts/setup_aws.sh [region]

set -euo pipefail

REGION="${1:-us-east-1}"
QUEUE_NAME="claude-bridge-commands"
TABLE_NAME="claude-bridge-results"
LAMBDA_ROLE_NAME="claude-bridge-lambda-role"

echo "=== Alexa-Claude Bridge AWS Setup ==="
echo "Region: $REGION"
echo ""

# ─── 1. SQS Queue ────────────────────────────────────────────────────
echo "Creating SQS queue: $QUEUE_NAME"
QUEUE_URL=$(aws sqs create-queue \
    --queue-name "$QUEUE_NAME" \
    --region "$REGION" \
    --attributes '{
        "VisibilityTimeout": "600",
        "MessageRetentionPeriod": "86400",
        "ReceiveMessageWaitTimeSeconds": "20"
    }' \
    --query 'QueueUrl' \
    --output text)
echo "  Queue URL: $QUEUE_URL"

QUEUE_ARN=$(aws sqs get-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --region "$REGION" \
    --attribute-names QueueArn \
    --query 'Attributes.QueueArn' \
    --output text)
echo "  Queue ARN: $QUEUE_ARN"

# ─── 2. DynamoDB Table ───────────────────────────────────────────────
echo ""
echo "Creating DynamoDB table: $TABLE_NAME"
aws dynamodb create-table \
    --table-name "$TABLE_NAME" \
    --region "$REGION" \
    --attribute-definitions \
        AttributeName=pk,AttributeType=S \
        AttributeName=sk,AttributeType=N \
    --key-schema \
        AttributeName=pk,KeyType=HASH \
        AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --query 'TableDescription.TableStatus' \
    --output text 2>/dev/null || echo "  (table may already exist)"

echo "  Table: $TABLE_NAME"

# ─── 3. IAM Role for Lambda ──────────────────────────────────────────
echo ""
echo "Creating IAM role: $LAMBDA_ROLE_NAME"

TRUST_POLICY='{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}'

ROLE_ARN=$(aws iam create-role \
    --role-name "$LAMBDA_ROLE_NAME" \
    --assume-role-policy-document "$TRUST_POLICY" \
    --query 'Role.Arn' \
    --output text 2>/dev/null || \
    aws iam get-role \
        --role-name "$LAMBDA_ROLE_NAME" \
        --query 'Role.Arn' \
        --output text)
echo "  Role ARN: $ROLE_ARN"

# Attach permissions
INLINE_POLICY='{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["sqs:SendMessage"],
            "Resource": "'"$QUEUE_ARN"'"
        },
        {
            "Effect": "Allow",
            "Action": ["dynamodb:Query", "dynamodb:PutItem", "dynamodb:GetItem"],
            "Resource": "arn:aws:dynamodb:'"$REGION"':*:table/'"$TABLE_NAME"'"
        },
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": "arn:aws:logs:'"$REGION"':*:*"
        }
    ]
}'

aws iam put-role-policy \
    --role-name "$LAMBDA_ROLE_NAME" \
    --policy-name "claude-bridge-permissions" \
    --policy-document "$INLINE_POLICY"

echo "  Permissions attached"

# ─── Summary ──────────────────────────────────────────────────────────
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Add these to your .env file:"
echo "  COMMAND_QUEUE_URL=$QUEUE_URL"
echo "  RESULTS_TABLE=$TABLE_NAME"
echo "  AWS_REGION=$REGION"
echo ""
echo "Lambda role ARN (use when creating Lambda function):"
echo "  $ROLE_ARN"
echo ""
echo "Next steps:"
echo "  1. Create Lambda function in AWS Console (or via CLI)"
echo "  2. Set Lambda handler to: handler.handler"
echo "  3. Set Lambda env vars: COMMAND_QUEUE_URL, RESULTS_TABLE"
echo "  4. Set Lambda role to: $ROLE_ARN"
echo "  5. Create Alexa skill at developer.amazon.com"
echo "  6. Upload skill/interaction_model.json"
echo "  7. Link skill to Lambda function"
echo "  8. Run local poller: uv run alexa-claude-poller"
