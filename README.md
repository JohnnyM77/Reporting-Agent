Bob the Bot

AI-powered announcements intelligence agent for my investment Portfolio.

Bob monitors ASX company announcements, downloads important documents, analyses them using AI, and delivers a daily intelligence digest.

What Bob Does

Bob automatically:

Monitors ASX announcements for Portfolio companies

Detects new announcements released in the last 48 hours

Classifies announcements by importance

Downloads PDFs where relevant

Uses AI to analyse major announcements

Stores important documents in Google Drive

Sends a clean daily email digest

Bob focuses on signal over noise.

Routine corporate filings are ignored.

Key Features
Smart Filtering

Bob only highlights announcements likely to impact valuation:

results

acquisitions

capital raises

debt refinancings

major contracts

trading updates

AI Analysis

Major announcements receive detailed analysis including:

financial impact

governance concerns

strategic implications

investor interpretation

Strawman Draft Posts

For major announcements Bob generates a ready-to-paste investor forum post.

Automatic Document Storage

Important PDFs are uploaded to Google Drive for later reference.

Portfolio Monitoring

Companies monitored are defined in:

tickers.yaml
Example Email Output
Bob the Bot
Daily Announcements Digest — last 2 days

HIGH IMPACT

DRO — FY Results
Revenue growth strong but margin compression continues.

Analysis:
[Detailed AI analysis here]

Open:
https://www.asx.com.au/...

Drive:
https://drive.google.com/...

MATERIAL

TWE: Retirement of CFO
So what: Leadership transition likely operational not strategic.

Open:
https://www.asx.com.au/...
Architecture

Core components:

agent.py

Main orchestration logic.

playwright_fetch.py

Browser automation used when ASX blocks direct PDF downloads.

prompts.py

AI prompts for announcement analysis.

tickers.yaml

Portfolio company list.

Workflow

Bob runs automatically via GitHub Actions.

Schedule example:

5 1 * * *

Runs daily.

Execution environment:

Python 3.11
GitHub hosted runner
Environment Variables

Required GitHub secrets:

EMAIL_FROM
EMAIL_TO
EMAIL_APP_PASSWORD
OPENAI_API_KEY
GDRIVE_SERVICE_ACCOUNT_JSON
GDRIVE_FOLDER_ID
BROTHER_EMAIL
Limits

Bob includes built-in guardrails:

MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15
MAX_ANNOUNCEMENTS_PER_TICKER = 12

These prevent runaway API costs.

Technology Stack

Python

Libraries used:

requests
beautifulsoup4
pypdf
playwright
openai
google-api-python-client
pyyaml
Deployment

Bob runs entirely in the cloud via:

GitHub Actions

Your computer does not need to be running.

Future Improvements

Potential upgrades:

support for LSE announcements

broker report ingestion

earnings model extraction

valuation tracking

Slack alerts

portfolio dashboards
