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

If you'd like, I can also show you 3 upgrades that would make Bob dramatically more powerful (the kind hedge funds actually build).

Can you spend a bit more time and update the ReadMe file so it's all one paste in one text box so it will look good in GitHub

Below is a clean, single-paste README.md designed to render nicely in GitHub.
It uses proper Markdown headings, code blocks, sections, and diagrams so it will look professional in your repo.

You can paste this directly into your README.md file.

Bob the Bot
Portfolio Announcements Intelligence Agent

Bob the Bot is an automated AI agent that monitors ASX company announcements for my investment portfolio, analyses important updates using AI, and delivers a daily intelligence briefing.

Bob's purpose is simple:

Surface the important information quickly and ignore the noise.

Instead of manually checking ASX announcements every day, Bob automatically reviews them, downloads relevant documents, runs analysis when needed, and emails a clean summary.

What Bob Does

Bob automatically performs the following tasks each day:

Monitors ASX announcements for companies in my Portfolio

Identifies new announcements released within the last 48 hours

Filters out routine corporate filings and administrative notices

Downloads important announcement PDFs

Extracts text and analyses the information using AI

Generates short summaries for quick review

Produces deeper analysis for major announcements

Saves important documents to Google Drive

Sends a daily intelligence digest via email

Bob is designed to maximise signal and minimise noise.

Example Output

Bob sends a daily email structured like this:

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

------------------------------------------------------------

MATERIAL

TWE: Retirement of Chief Financial Officer
So what: Leadership transition likely operational rather than strategic.

Open:
https://www.asx.com.au/...

------------------------------------------------------------

FYI

ABB: Appendix 3Y Director Interest Notice
So what: Routine filing.
Portfolio Monitoring

Companies monitored are defined in:

tickers.yaml

Example:

asx:
  - DRO
  - AR9
  - CAT
  - ABB
  - TWE

Bob loops through each ticker and retrieves announcements from the ASX website.

Announcement Sources

Bob retrieves announcements directly from the official ASX announcement system:

https://www.asx.com.au/asx/v2/statistics/announcements.do

For example:

https://www.asx.com.au/asx/v2/statistics/announcements.do?asxCode=DRO

The following information is extracted:

Announcement title

Release date and time

Announcement URL

PDF link (if available)

Only announcements released within the last 2 days are processed.

Announcement Classification

Each announcement is automatically classified.

Categories include:

Category	Description
RESULTS_HY_FY	Half year or full year results
ACQUISITION	M&A transactions
CAPITAL_OR_DEBT_RAISE	Equity issuance or debt financing
CONTRACT_MATERIAL	Major contracts or strategic partnerships
OTHER	Routine corporate updates

Classification is based primarily on headline keyword detection.

Document Retrieval

When an announcement contains useful information, Bob attempts to download the PDF.

Retrieval process:

Step 1 — Direct Download

Bob first attempts a normal download using the Python requests library.

Step 2 — Browser Simulation

If the ASX legal consent page blocks the request, Bob uses:

Playwright

to simulate a real browser and retrieve the document.

Step 3 — HTML Fallback

If the PDF cannot be retrieved, Bob extracts text directly from the announcement webpage.

Text Extraction

When a PDF is successfully downloaded, Bob extracts the text using:

pypdf

Bob also performs safety checks to ensure the extracted content is meaningful.

These checks prevent analysis of:

empty documents

ASX legal disclaimer pages

broken downloads

AI Analysis

Bob uses the OpenAI API to analyse announcements.

Default model:

gpt-4o-mini

AI is used selectively to avoid unnecessary costs.

Deep Analysis Triggers

Bob performs detailed analysis only for major announcements:

Half year results

Full year results

Acquisitions

Capital raises

Debt refinancings

These announcements produce:

a full AI analysis

a Strawman-ready investor summary

stored documents in Google Drive

Quick Summaries

Most announcements receive a quick two-line summary:

Example:

DRO: $21.7m Western Military Contracts
So what: Adds to defence pipeline but unlikely to move revenue materially this year.
Google Drive Storage

Important documents are uploaded to Google Drive automatically.

This allows later review of:

results reports

investor presentations

acquisition documents

capital raise materials

Drive links are included in the email digest.

Email Alerts

Bob produces a daily Portfolio intelligence email.

Sections include:

HIGH IMPACT

Major announcements requiring deep analysis:

results

acquisitions

capital raises

MATERIAL

Price-sensitive announcements requiring attention.

These receive short AI summaries.

FYI

Routine announcements included for completeness.

SILENCE

If no announcements occurred:

No announcements in the last 2 days.
Special Routing

Certain tickers trigger additional alerts.

Example:

AR9

If AR9 appears in the announcements:

Bob sends a separate email digest to my brother.

This email contains:

only AR9 announcements

simplified summaries

Architecture

Core components:

agent.py

Main orchestration logic.

playwright_fetch.py

Browser automation used when ASX blocks direct downloads.

prompts.py

AI prompt templates used for analysis.

tickers.yaml

Portfolio company list.

Workflow

Bob runs automatically via GitHub Actions.

Example schedule:

5 1 * * *

This corresponds to:

09:05 Singapore Time

Execution environment:

GitHub Cloud Runner
Python 3.11

Your computer does not need to be running.

Environment Variables

Required GitHub Secrets:

EMAIL_FROM
EMAIL_TO
EMAIL_APP_PASSWORD
OPENAI_API_KEY
GDRIVE_SERVICE_ACCOUNT_JSON
GDRIVE_FOLDER_ID
BROTHER_EMAIL

These enable:

email delivery

AI analysis

Google Drive uploads

special alert routing

Safety Limits

To prevent runaway API usage, Bob includes built-in limits:

MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15
MAX_ANNOUNCEMENTS_PER_TICKER = 12
Technology Stack

Bob is written in Python.

Key libraries:

requests
beautifulsoup4
pypdf
playwright
openai
google-api-python-client
pyyaml
Deployment

Bob runs entirely in the cloud using:

GitHub Actions

This means:

Bob runs automatically every day

Your computer does not need to be on

Execution logs are available in the GitHub Actions tab

Future Improvements

Potential upgrades include:

monitoring LSE announcements

broker report ingestion

earnings model extraction

portfolio valuation tracking

Slack or Telegram alerts

dashboard visualisation

insider transaction detection

Why Bob Exists

The ASX produces hundreds of announcements each week.

Most are irrelevant.

Bob's job is to read everything so I don't have to, highlight the important information, and deliver a concise daily briefing for my investment portfolio.
