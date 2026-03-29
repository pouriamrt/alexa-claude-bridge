# ── Alexa-Claude Bridge Makefile ──────────────────────────────────────
#
# Workflow:
#   make setup           — first-time: AWS infra + deps + bridge config
#   make deploy          — package and deploy Lambda + Alexa skill model
#   make start           — activate bridge (run before/during a Claude session)
#   make stop            — deactivate bridge
#   make teardown        — destroy all AWS resources
#
# Prerequisites: aws (configured), uv, ask-cli (optional, for skill deploy)
# ──────────────────────────────────────────────────────────────────────

SHELL := bash
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

# ── Config (override via env or .env) ─────────────────────────────────
AWS_REGION       ?= us-east-1
QUEUE_NAME       ?= claude-bridge-commands
TABLE_NAME       ?= claude-bridge-results
LAMBDA_NAME      ?= claude-bridge-alexa
LAMBDA_ROLE_NAME ?= claude-bridge-lambda-role
LAMBDA_RUNTIME   ?= python3.13
LAMBDA_TIMEOUT   ?= 15
SKILL_ID         ?= $(shell cat .skill-id 2>/dev/null)

# ── Derived ───────────────────────────────────────────────────────────
BUILD_DIR    := .build
LAMBDA_ZIP   := $(BUILD_DIR)/lambda.zip
ENV_FILE     := .env
BRIDGE_DIR   := ~/.claude-bridge

# ======================================================================
#  High-level targets
# ======================================================================

.PHONY: setup deploy start stop status logs teardown clean help
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: infra env install bridge-install ## First-time: AWS + deps + bridge config
	@echo ""
	@echo "=== Setup complete ==="
	@echo "Next: make deploy, then create Alexa skill (see README)"

deploy: lambda-deploy ## Deploy Lambda (+ Alexa skill if .skill-id exists)
	@echo ""
	@echo "=== Deploy complete ==="
	@if [ -n "$(SKILL_ID)" ]; then \
		echo "Updating Alexa skill model..."; \
		$(MAKE) skill-deploy; \
	else \
		echo "No .skill-id found — upload skill/interaction_model.json manually"; \
		echo "Then: echo 'amzn1.ask.skill.xxx' > .skill-id"; \
	fi

# ======================================================================
#  Bridge lifecycle
# ======================================================================

start: ## Activate bridge — daemon starts, Claude notifies Alexa when done
	@uv run alexa-bridge start

stop: ## Deactivate bridge — daemon stops, notifications disabled
	@uv run alexa-bridge stop

status: ## Show bridge status (active/inactive, daemon PID, notify config)
	@uv run alexa-bridge status

logs: ## Tail the background daemon log
	@uv run alexa-bridge logs

bridge-install: ## One-time: create config, notify script, add CLAUDE.md rule
	@uv run alexa-bridge install

# ======================================================================
#  Infrastructure (AWS)
# ======================================================================

.PHONY: infra infra-sqs infra-dynamodb infra-iam

infra: infra-sqs infra-dynamodb infra-iam ## Create all AWS resources
	@echo ""
	@echo "=== Infrastructure ready ==="

infra-sqs: ## Create SQS queue
	@echo "Creating SQS queue: $(QUEUE_NAME)"
	@QUEUE_URL=$$(aws sqs create-queue \
		--queue-name "$(QUEUE_NAME)" \
		--region "$(AWS_REGION)" \
		--attributes '{"VisibilityTimeout":"600","MessageRetentionPeriod":"86400","ReceiveMessageWaitTimeSeconds":"20"}' \
		--query 'QueueUrl' --output text) && \
	echo "  $$QUEUE_URL"

infra-dynamodb: ## Create DynamoDB table
	@echo "Creating DynamoDB table: $(TABLE_NAME)"
	@aws dynamodb create-table \
		--table-name "$(TABLE_NAME)" \
		--region "$(AWS_REGION)" \
		--attribute-definitions \
			AttributeName=pk,AttributeType=S \
			AttributeName=sk,AttributeType=N \
		--key-schema \
			AttributeName=pk,KeyType=HASH \
			AttributeName=sk,KeyType=RANGE \
		--billing-mode PAY_PER_REQUEST \
		--query 'TableDescription.TableStatus' \
		--output text 2>/dev/null || echo "  (already exists)"

infra-iam: ## Create Lambda IAM role + policy
	@echo "Creating IAM role: $(LAMBDA_ROLE_NAME)"
	@ROLE_ARN=$$(aws iam create-role \
		--role-name "$(LAMBDA_ROLE_NAME)" \
		--assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
		--query 'Role.Arn' --output text 2>/dev/null || \
		aws iam get-role --role-name "$(LAMBDA_ROLE_NAME)" --query 'Role.Arn' --output text) && \
	echo "  $$ROLE_ARN"
	@QUEUE_ARN=$$(aws sqs get-queue-attributes \
		--queue-url "$$(aws sqs get-queue-url --queue-name $(QUEUE_NAME) --region $(AWS_REGION) --query 'QueueUrl' --output text)" \
		--region "$(AWS_REGION)" \
		--attribute-names QueueArn \
		--query 'Attributes.QueueArn' --output text) && \
	aws iam put-role-policy \
		--role-name "$(LAMBDA_ROLE_NAME)" \
		--policy-name "claude-bridge-permissions" \
		--policy-document "$$(cat <<-POLICY
		{"Version":"2012-10-17","Statement":[
			{"Effect":"Allow","Action":["sqs:SendMessage"],"Resource":"$$QUEUE_ARN"},
			{"Effect":"Allow","Action":["dynamodb:Query","dynamodb:PutItem","dynamodb:GetItem"],"Resource":"arn:aws:dynamodb:$(AWS_REGION):*:table/$(TABLE_NAME)"},
			{"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:$(AWS_REGION):*:*"}
		]}
		POLICY
		)" && \
	echo "  Permissions attached"

# ======================================================================
#  .env generation
# ======================================================================

.PHONY: env

env: ## Generate .env from live AWS resources
	@echo "Generating $(ENV_FILE)..."
	@QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(QUEUE_NAME)" \
		--region "$(AWS_REGION)" \
		--query 'QueueUrl' --output text) && \
	cat > $(ENV_FILE) <<-EOF
	COMMAND_QUEUE_URL=$$QUEUE_URL
	RESULTS_TABLE=$(TABLE_NAME)
	AWS_REGION=$(AWS_REGION)
	EOF
	@echo "  Written to $(ENV_FILE)"

# ======================================================================
#  Local install
# ======================================================================

.PHONY: install lint

install: ## Install Python dependencies
	@uv sync

lint: ## Lint all source files
	@uv run ruff check src/ lambda/
	@uv run ruff format --check src/ lambda/

# ======================================================================
#  Lambda packaging & deploy
# ======================================================================

.PHONY: lambda-package lambda-deploy

$(BUILD_DIR):
	@mkdir -p $(BUILD_DIR)

lambda-package: $(BUILD_DIR) ## Zip the Lambda handler (boto3 is in the runtime)
	@echo "Packaging Lambda..."
	@cp lambda/handler.py $(BUILD_DIR)/handler.py
	@cd $(BUILD_DIR) && zip -q lambda.zip handler.py
	@echo "  $(LAMBDA_ZIP) ($$(du -h $(LAMBDA_ZIP) | cut -f1))"

lambda-deploy: lambda-package ## Create or update the Lambda function
	@ROLE_ARN=$$(aws iam get-role \
		--role-name "$(LAMBDA_ROLE_NAME)" \
		--query 'Role.Arn' --output text) && \
	QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(QUEUE_NAME)" \
		--region "$(AWS_REGION)" \
		--query 'QueueUrl' --output text) && \
	if aws lambda get-function --function-name "$(LAMBDA_NAME)" --region "$(AWS_REGION)" >/dev/null 2>&1; then \
		echo "Updating Lambda: $(LAMBDA_NAME)"; \
		aws lambda update-function-code \
			--function-name "$(LAMBDA_NAME)" \
			--region "$(AWS_REGION)" \
			--zip-file "fileb://$(LAMBDA_ZIP)" \
			--query 'FunctionArn' --output text; \
	else \
		echo "Creating Lambda: $(LAMBDA_NAME)"; \
		aws lambda create-function \
			--function-name "$(LAMBDA_NAME)" \
			--region "$(AWS_REGION)" \
			--runtime "$(LAMBDA_RUNTIME)" \
			--role "$$ROLE_ARN" \
			--handler "handler.handler" \
			--timeout $(LAMBDA_TIMEOUT) \
			--zip-file "fileb://$(LAMBDA_ZIP)" \
			--environment "Variables={COMMAND_QUEUE_URL=$$QUEUE_URL,RESULTS_TABLE=$(TABLE_NAME)}" \
			--query 'FunctionArn' --output text; \
		echo "Adding Alexa trigger permission..."; \
		aws lambda add-permission \
			--function-name "$(LAMBDA_NAME)" \
			--region "$(AWS_REGION)" \
			--statement-id "alexa-skill-invoke" \
			--action "lambda:InvokeFunction" \
			--principal "alexa-appkit.amazon.com" \
			--query 'Statement' --output text 2>/dev/null || true; \
	fi

lambda-logs: ## Tail Lambda CloudWatch logs
	@aws logs tail "/aws/lambda/$(LAMBDA_NAME)" \
		--region "$(AWS_REGION)" --follow --format short

# ======================================================================
#  Alexa skill
# ======================================================================

.PHONY: skill-deploy skill-status

skill-deploy: ## Update Alexa skill interaction model (requires .skill-id)
	@if [ -z "$(SKILL_ID)" ]; then \
		echo "ERROR: No .skill-id file. Create skill at developer.amazon.com first,"; \
		echo "then: echo 'amzn1.ask.skill.xxx' > .skill-id"; \
		exit 1; \
	fi
	@echo "Updating skill model for $(SKILL_ID)..."
	@ask smapi update-interaction-model \
		--skill-id "$(SKILL_ID)" \
		--stage development \
		--locale en-US \
		--interaction-model "file:skill/interaction_model.json"
	@echo "  Skill model updated"

skill-status: ## Check Alexa skill build status
	@if [ -z "$(SKILL_ID)" ]; then echo "No .skill-id"; exit 1; fi
	@ask smapi get-skill-status --skill-id "$(SKILL_ID)"

# ======================================================================
#  Testing & debugging
# ======================================================================

.PHONY: test-send test-result queue-status

test-send: ## Send a test command to SQS (usage: make test-send CMD="check git status")
	@CMD="$${CMD:-hello}"; \
	QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(QUEUE_NAME)" \
		--region "$(AWS_REGION)" \
		--query 'QueueUrl' --output text) && \
	aws sqs send-message \
		--queue-url "$$QUEUE_URL" \
		--region "$(AWS_REGION)" \
		--message-body "$$(python -c "import json,uuid,time; print(json.dumps({'command_id':str(uuid.uuid4()),'command':'$$CMD','timestamp':int(time.time())}))")" \
		--query 'MessageId' --output text && \
	echo "Sent: $$CMD"

test-result: ## Read the latest result from DynamoDB
	@aws dynamodb query \
		--table-name "$(TABLE_NAME)" \
		--region "$(AWS_REGION)" \
		--key-condition-expression "pk = :pk" \
		--expression-attribute-values '{":pk":{"S":"user#default"}}' \
		--scan-index-forward false \
		--limit 1 \
		--query 'Items[0].{summary:summary.S,timestamp:timestamp.S}' \
		--output table

queue-status: ## Show SQS queue depth
	@QUEUE_URL=$$(aws sqs get-queue-url \
		--queue-name "$(QUEUE_NAME)" \
		--region "$(AWS_REGION)" \
		--query 'QueueUrl' --output text) && \
	aws sqs get-queue-attributes \
		--queue-url "$$QUEUE_URL" \
		--region "$(AWS_REGION)" \
		--attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible \
		--output table

# ======================================================================
#  Teardown
# ======================================================================

.PHONY: teardown

teardown: stop ## Destroy all AWS resources + local bridge config (IRREVERSIBLE)
	@echo "WARNING: This will delete the SQS queue, DynamoDB table, Lambda, IAM role,"
	@echo "         and local bridge config (~/.claude-bridge/)."
	@read -p "Type 'yes' to confirm: " CONFIRM && [ "$$CONFIRM" = "yes" ] || exit 1
	@echo ""
	@echo "Deleting Lambda..."
	@aws lambda delete-function --function-name "$(LAMBDA_NAME)" --region "$(AWS_REGION)" 2>/dev/null || true
	@echo "Deleting SQS queue..."
	@QUEUE_URL=$$(aws sqs get-queue-url --queue-name "$(QUEUE_NAME)" --region "$(AWS_REGION)" --query 'QueueUrl' --output text 2>/dev/null) && \
		aws sqs delete-queue --queue-url "$$QUEUE_URL" --region "$(AWS_REGION)" 2>/dev/null || true
	@echo "Deleting DynamoDB table..."
	@aws dynamodb delete-table --table-name "$(TABLE_NAME)" --region "$(AWS_REGION)" 2>/dev/null || true
	@echo "Deleting IAM role..."
	@aws iam delete-role-policy --role-name "$(LAMBDA_ROLE_NAME)" --policy-name "claude-bridge-permissions" 2>/dev/null || true
	@aws iam delete-role --role-name "$(LAMBDA_ROLE_NAME)" 2>/dev/null || true
	@echo "Removing local bridge config..."
	@rm -rf $(BRIDGE_DIR)
	@echo "Done. All resources removed."

# ======================================================================
#  Cleanup
# ======================================================================

clean: ## Remove build artifacts
	@rm -rf $(BUILD_DIR)
