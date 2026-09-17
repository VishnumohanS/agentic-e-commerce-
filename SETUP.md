# SETUP

Everything you need to do after unzipping the project. It assumes no prior AWS knowledge.

The project runs **completely offline by default** — no AWS account and no Razorpay account are needed to see a full purchase work. Parts 1–4 get you there. Parts 5 onward are for when you want real AWS Bedrock and real Razorpay test payments.

---

## Contents

1. [Requirements](#1-requirements)
2. [Installation](#2-installation)
3. [Environment configuration](#3-environment-configuration)
4. [Run it offline](#4-run-it-offline-no-accounts-needed)
5. [AWS setup](#5-aws-setup)
6. [Bedrock setup](#6-bedrock-setup)
7. [Razorpay setup](#7-razorpay-setup)
8. [Docker](#8-docker-optional)
9. [Deploy to AWS](#9-deploy-to-aws)
10. [Verifying everything works](#10-verifying-everything-works)

---

## 1. Requirements

| Requirement | Version | Needed for |
|---|---|---|
| Python | 3.11 or newer (3.12 recommended) | Everything |
| pip | Bundled with Python | Everything |
| Git | Any | Optional |
| AWS account | — | Only for Bedrock / deployment |
| AWS CLI | v2 | Only for Bedrock / deployment |
| Razorpay account | Free | Only for real test payments |
| Docker Desktop | Any recent | Only for the Docker path |

Check your Python version:

```bash
python --version
```

If that prints 3.10 or lower, try `python3 --version` and use `python3` throughout, or install a newer Python from <https://www.python.org/downloads/>.

---

## 2. Installation

### macOS / Linux

```bash
cd agentic-commerce-platform
python -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Windows PowerShell

```powershell
cd agentic-commerce-platform
python -m venv venv
venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

You should see `(venv)` at the start of your prompt. The virtualenv must be active in **every** terminal you use.

Optional — only if you want the stdio MCP server for MCP-native clients:

```bash
pip install -r requirements-optional.txt
```

---

## 3. Environment configuration

Copy the template:

```bash
cp .env.example .env
```

```powershell
Copy-Item .env.example .env
```

**Never commit `.env`.** It is already listed in `.gitignore`.

The defaults work offline as-is. The one value you should change before doing anything real is the mandate signing secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Paste the output into `AP2_MANDATE_SECRET` in `.env`.

### Variable reference

Marked **REQUIRED** (must have a value), **OPTIONAL**, **LOCAL ONLY** (never set in production) or **PRODUCTION**.

#### Application

| Variable | Status | Meaning |
|---|---|---|
| `ENVIRONMENT` | REQUIRED | `development`, `test` or `production`. Production enables JSON logging and makes Secrets Manager failures fatal instead of silent. |
| `LOG_LEVEL` | OPTIONAL | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default `INFO`. |
| `LOG_FORMAT` | OPTIONAL | `console` locally, `json` for CloudWatch. |

#### AI provider

| Variable | Status | Meaning |
|---|---|---|
| `AI_PROVIDER` | REQUIRED | `mock` runs a deterministic offline provider. `bedrock` calls AWS. |
| `BEDROCK_MODEL_ID` | REQUIRED with Bedrock | The chat model, e.g. `anthropic.claude-3-5-sonnet-20240620-v1:0`. |
| `BEDROCK_EMBEDDING_MODEL_ID` | REQUIRED with Bedrock | e.g. `amazon.titan-embed-text-v2:0`. |
| `BEDROCK_MAX_TOKENS`, `BEDROCK_TEMPERATURE` | OPTIONAL | Generation limits. Temperature defaults to 0 for reproducibility. |
| `BEDROCK_GUARDRAIL_ID`, `BEDROCK_GUARDRAIL_VERSION` | OPTIONAL | Attach a Bedrock Guardrail to the buyer agent's reasoning. |

#### AWS

| Variable | Status | Meaning |
|---|---|---|
| `AWS_REGION` | REQUIRED with Bedrock | Must be a region where you enabled model access. `ap-south-1` (Mumbai) is a sensible default for India; change it freely. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | LOCAL ONLY | Prefer `aws configure` or `AWS_PROFILE`. **Leave both empty in production** — ECS task roles provide credentials automatically and long-lived keys should not exist in a deployed container. |
| `AWS_PROFILE` | OPTIONAL, LOCAL ONLY | A named profile from `~/.aws/credentials`. |

#### Razorpay

| Variable | Status | Meaning |
|---|---|---|
| `RAZORPAY_MODE` | REQUIRED | `mock` for the offline simulator, `test` for the real test API. |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | REQUIRED with `test` | Test-mode credentials. Keys not starting with `rzp_test_` are refused at startup. |
| `RAZORPAY_WEBHOOK_SECRET` | OPTIONAL | Only needed if you configure a webhook. |

#### AP2 mandates

| Variable | Status | Meaning |
|---|---|---|
| `AP2_MANDATE_SECRET` | REQUIRED | HMAC signing key. Generate locally; store in Secrets Manager for production. |
| `AP2_MANDATE_TTL_SECONDS` | OPTIONAL | Mandate lifetime, default 900 (15 minutes). |
| `AP2_ABSOLUTE_MAX_AMOUNT` | OPTIONAL | Platform-wide ceiling in paise. Default `500000` = ₹5,000. No mandate can exceed this, whatever the user asks for. |

#### Storage

| Variable | Status | Meaning |
|---|---|---|
| `LEDGER_BACKEND` | REQUIRED | `sqlite` locally, `dynamodb` in production. |
| `DATABASE_URL`, `BUYER_DATABASE_URL` | LOCAL ONLY | SQLite file paths for the two ledgers. |
| `DYNAMODB_TABLE_NAME` | PRODUCTION | Created by CloudFormation as `acp-audit-ledger`. |
| `DYNAMODB_ENDPOINT_URL` | OPTIONAL | Point at DynamoDB Local for offline testing. |

#### Security and networking

| Variable | Status | Meaning |
|---|---|---|
| `A2A_API_KEY` | REQUIRED | Shared secret for buyer → merchant calls. Both agents must agree. |
| `SECRETS_MANAGER_SECRET_NAME` | PRODUCTION | JSON secret bundle overlaid over `.env` at startup. |
| `KMS_KEY_ID` | OPTIONAL | Customer-managed key for table encryption. |
| `MERCHANT_AGENT_URL`, `BUYER_AGENT_URL` | REQUIRED | Defaults suit local runs. |
| `MERCHANT_AGENT_PORT`, `BUYER_AGENT_PORT`, `HTTP_TIMEOUT_SECONDS` | OPTIONAL | Ports and client timeout. |

---

## 4. Run it offline (no accounts needed)

Two terminals, both with the virtualenv active.

**Terminal 1 — merchant agent:**

```bash
uvicorn merchant_agent.main:app --port 8000
```

**Terminal 2 — buyer agent:**

```bash
uvicorn buyer_agent.main:app --port 8001
```

Open <http://localhost:8001>, type *"I need good wireless headphones under 2500 rupees"* and press **Start purchase**. You should see the agent's reasoning stream in, then the product, mandate, upsell decision, payment result and the audit ledger.

Windows PowerShell is identical (`uvicorn` is on the path once the venv is active).

Prefer one command?

```bash
python scripts/smoke_test.py
```

That runs the whole pipeline plus the failure paths in a single process and prints a pass/fail report.

---

## 5. AWS setup

Skip this section entirely if you are happy running offline.

### 5.1 Install the AWS CLI

Download AWS CLI v2 from <https://aws.amazon.com/cli/> and verify:

```bash
aws --version
```

### 5.2 Create an access key

1. Sign in to the AWS Console.
2. Click your account name (top right) → **Security credentials**.
3. Scroll to **Access keys** → **Create access key**.
4. Choose **Command Line Interface (CLI)**, tick the acknowledgement, **Next**, **Create access key**.
5. Copy both the Access key ID and the Secret access key. The secret is shown once.

For a student project an admin user is acceptable. For anything real, create an IAM user with only the permissions listed in 5.4.

### 5.3 Configure credentials

```bash
aws configure
```

Answer:

```text
AWS Access Key ID     : <your key id>
AWS Secret Access Key : <your secret>
Default region name   : ap-south-1
Default output format : json
```

Verify:

```bash
aws sts get-caller-identity
```

This writes credentials to `~/.aws/credentials`, which is why you should leave `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` empty in `.env`.

### 5.4 Which region

Use a region that offers the Bedrock models you want. `ap-south-1` (Mumbai), `us-east-1` (N. Virginia) and `us-west-2` (Oregon) are common choices; `us-east-1` has the widest model selection. Whatever you pick, use the **same region** in `.env`, in the CLI and in the Bedrock console.

### 5.5 Required IAM permissions

The CloudFormation template creates a least-privilege task role for you:

| Permission | Resource | Why |
|---|---|---|
| `bedrock:InvokeModel`, `bedrock:Converse` | Only the two model ARNs | Reasoning and embeddings |
| `dynamodb:PutItem`, `Query`, `GetItem` | Ledger table + its index | Append and read audit events. `UpdateItem`/`DeleteItem` are deliberately **not** granted — the ledger is append-only |
| `secretsmanager:GetSecretValue` | The one `acp/app` secret | Load production secrets |
| `cloudwatch:PutMetricData` | The project's metric namespace | Custom metrics |
| `logs:CreateLogStream`, `PutLogEvents` | Task log groups | Container logs (via the managed execution-role policy) |

Your own user additionally needs permission to deploy the stack (CloudFormation, ECS, ECR, IAM, DynamoDB, Secrets Manager, ELB, EC2 security groups).

### 5.6 DynamoDB tables

One table, created by the template:

| Property | Value |
|---|---|
| Name | `acp-audit-ledger` |
| Partition key | `chain_id` (String) |
| Sort key | `sequence` (Number) |
| GSI | `transaction_id-index` on `transaction_id` |
| Billing | On-demand (pay per request) |
| Encryption | Enabled |
| Point-in-time recovery | Enabled |

To create it by hand instead:

```bash
aws dynamodb create-table \
  --table-name acp-audit-ledger \
  --attribute-definitions AttributeName=chain_id,AttributeType=S \
                          AttributeName=sequence,AttributeType=N \
                          AttributeName=transaction_id,AttributeType=S \
  --key-schema AttributeName=chain_id,KeyType=HASH \
               AttributeName=sequence,KeyType=RANGE \
  --global-secondary-indexes '[{"IndexName":"transaction_id-index","KeySchema":[{"AttributeName":"transaction_id","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}}]' \
  --billing-mode PAY_PER_REQUEST \
  --region ap-south-1
```

### 5.7 Secrets Manager

One secret named `acp/app`, holding JSON:

```json
{
  "RAZORPAY_KEY_ID": "rzp_test_xxxxxxxx",
  "RAZORPAY_KEY_SECRET": "xxxxxxxx",
  "RAZORPAY_WEBHOOK_SECRET": "xxxxxxxx",
  "AP2_MANDATE_SECRET": "your_generated_hex_string",
  "A2A_API_KEY": "another_generated_string"
}
```

Fill it in after deploying:

```bash
aws secretsmanager put-secret-value \
  --secret-id acp/app \
  --secret-string file://secret.json \
  --region ap-south-1
```

Delete `secret.json` afterwards. At startup the application overlays these values over `.env`, so production containers hold no secrets on disk.

### 5.8 CloudWatch

The template creates:

- Log groups `/ecs/acp/merchant-agent` and `/ecs/acp/buyer-agent`, 14-day retention.
- A metric filter counting payment verification failures.
- A metric filter counting mandate rejections.
- An alarm firing when payment failures exceed 5 in 5 minutes.

With `LOG_FORMAT=json` each line is a JSON object carrying `transaction_id`, `event_type`, `agent`, `status`, `duration_ms` and `error_type`, so you can query in Logs Insights:

```text
fields @timestamp, transaction_id, error_code, message
| filter error_code like /mandate/
| sort @timestamp desc
```

Secrets never reach the logs: a redaction filter scrubs known secret values and sensitive field names before anything is emitted.

---

## 6. Bedrock setup

### 6.1 Request model access

1. Open the AWS Console and switch to your chosen region (top-right region picker).
2. Search for **Bedrock** and open it.
3. In the left sidebar choose **Model access**.
4. Click **Modify model access** (or **Manage model access**).
5. Tick the models you need:
   - A chat model — **Claude 3.5 Sonnet** (or **Amazon Nova Lite**, which is cheaper).
   - An embedding model — **Titan Text Embeddings V2**.
6. Click **Next**, then **Submit**. Amazon models are usually granted instantly; Anthropic models may ask for a short use-case description and can take a few minutes.
7. Wait until the status reads **Access granted**.

### 6.2 Point the project at Bedrock

In `.env`:

```env
AI_PROVIDER=bedrock
AWS_REGION=ap-south-1
BEDROCK_MODEL_ID=anthropic.claude-3-5-sonnet-20240620-v1:0
BEDROCK_EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
```

Confirm the exact model ids for your region:

```bash
aws bedrock list-foundation-models --region ap-south-1 \
  --query "modelSummaries[].modelId" --output table
```

Restart both agents and check the UI status line — it should now read `bedrock`.

### 6.3 Optional: guardrails

Bedrock console → **Guardrails** → **Create guardrail**, then put its id and version in `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION`. Guardrails are an extra AWS-managed layer on the buyer agent's reasoning; the deterministic AP2 checks remain the actual spending control.

---

## 7. Razorpay setup

### 7.1 Create an account and switch to Test Mode

1. Sign up at <https://dashboard.razorpay.com/signup>.
2. In the dashboard, flip the **Test Mode** toggle (top of the sidebar). Every screen should now show a "Test Mode" indicator.

### 7.2 Get your test keys

1. Go to **Account & Settings** → **API Keys** (older layouts: **Settings** → **API Keys**).
2. Click **Generate Test Key**.
3. Copy the **Key Id** (`rzp_test_...`) and the **Key Secret**. The secret is shown once — download or copy it now.

In `.env`:

```env
RAZORPAY_MODE=test
RAZORPAY_KEY_ID=rzp_test_your_key_here
RAZORPAY_KEY_SECRET=your_secret_here
```

The application refuses to start if the key id does not begin with `rzp_test_`, so live credentials cannot be used by accident.

### 7.3 Optional: webhook

1. **Account & Settings** → **Webhooks** → **Add New Webhook**.
2. URL: `https://your-public-host/webhooks/razorpay` (locally, expose port 8000 with a tunnel such as ngrok).
3. Choose a secret, and set the same value in `RAZORPAY_WEBHOOK_SECRET`.
4. Subscribe to `payment.captured` and `payment.failed`.

Webhooks are treated as notifications only — the authoritative check is still signature plus status verification during confirmation. Unsigned webhooks are rejected with 401 before the body is parsed.

### 7.4 How to test a payment

In `mock` mode the buyer agent completes checkout itself, so a purchase runs end to end with no interaction.

In `test` mode, Razorpay's test cards apply, for example card `4111 1111 1111 1111`, any future expiry, any CVV, OTP `1234`. Nothing is ever charged; you can review every test order in the dashboard under **Transactions** → **Orders**.

---

## 8. Docker (optional)

Docker is not required. If you want it:

```bash
cp .env.example .env
docker compose up --build
```

The UI is on <http://localhost:8001>. Stop with `Ctrl+C`, then `docker compose down`.

---

## 9. Deploy to AWS

### 9.1 Deploy the stack

Find your default VPC and two subnets:

```bash
aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query "Vpcs[0].VpcId" --output text --region ap-south-1

aws ec2 describe-subnets --filters Name=vpc-id,Values=<vpc-id> \
  --query "Subnets[].SubnetId" --output text --region ap-south-1
```

Deploy:

```bash
aws cloudformation deploy \
  --template-file infrastructure/cloudformation.yaml \
  --stack-name acp \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ap-south-1 \
  --parameter-overrides \
      VpcId=<vpc-id> \
      SubnetIds=<subnet-1>\\,<subnet-2> \
      DesiredCount=0
```

Starting with `DesiredCount=0` avoids paying for tasks that would crash before the secret is filled in.

### 9.2 Fill in the secret

Use the JSON from section 5.7 and `put-secret-value`.

### 9.3 Build and push the images

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=ap-south-1

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build -t acp:latest .

docker tag acp:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/acp-merchant-agent:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/acp-merchant-agent:latest

docker tag acp:latest $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/acp-buyer-agent:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/acp-buyer-agent:latest
```

On Apple Silicon, add `--platform linux/amd64` to `docker build`.

### 9.4 Start the services

```bash
aws cloudformation deploy \
  --template-file infrastructure/cloudformation.yaml \
  --stack-name acp \
  --capabilities CAPABILITY_NAMED_IAM \
  --region ap-south-1 \
  --parameter-overrides VpcId=<vpc-id> SubnetIds=<subnet-1>\\,<subnet-2> DesiredCount=1
```

Get the URL:

```bash
aws cloudformation describe-stacks --stack-name acp --region ap-south-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApplicationUrl'].OutputValue" --output text
```

### 9.5 What is automatic vs manual

**Automatic:** DynamoDB table and index, Secrets Manager secret shell, both ECR repositories, CloudWatch log groups, metric filters and alarm, IAM execution and task roles, security groups, ALB, target group, listener, ECS cluster, task definitions, services, private DNS service discovery.

**Manual:** enabling Bedrock model access, putting real values in the secret, building and pushing images, setting `DesiredCount`, and configuring a Razorpay webhook if you want one.

### 9.6 Shutting down

```bash
# Stop paying for tasks but keep the stack
aws cloudformation deploy --template-file infrastructure/cloudformation.yaml \
  --stack-name acp --capabilities CAPABILITY_NAMED_IAM --region ap-south-1 \
  --parameter-overrides VpcId=<vpc-id> SubnetIds=<subnet-1>\\,<subnet-2> DesiredCount=0

# Or remove everything (delete images from ECR first)
aws cloudformation delete-stack --stack-name acp --region ap-south-1
```

The load balancer bills hourly even when idle, so delete the stack when you are done demonstrating.

---

## 10. Verifying everything works

```bash
pytest -v                      # 211 tests, all offline
python scripts/smoke_test.py   # full pipeline + failure paths
```

Manual checks with both agents running:

```bash
curl http://localhost:8000/health
curl http://localhost:8001/health
curl http://localhost:8000/catalog                     # UCP JSON-LD
curl http://localhost:8000/.well-known/agent.json      # A2A agent card
curl http://localhost:8000/ledger/verify               # hash chain check

curl -X POST http://localhost:8001/purchase \
  -H "Content-Type: application/json" \
  -d '{"request": "wireless headphones under 2500 rupees"}'
```

A successful response ends with `"status": "completed"` and a confirmation whose `amount` never exceeds the mandate's `maximum_amount`.

To see the tamper detection for yourself, edit a row in `data/merchant_ledger.db` with any SQLite browser, then call `/ledger/verify` again — it will report the exact sequence number where the chain breaks.
