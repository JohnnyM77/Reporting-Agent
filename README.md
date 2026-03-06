Bob the Bot

Portfolio Announcements Intelligence Agent

Bob the Bot is an automated AI agent that monitors ASX announcements for companies in my investment Portfolio, analyses important updates, and delivers a daily intelligence briefing.

The goal is simple:

Read everything so I don't have to. Surface the signal and ignore the noise.

Instead of manually checking ASX announcements every day, Bob retrieves new filings, downloads important documents, runs AI analysis when appropriate, and sends a concise summary.

System Overview
                ┌──────────────────────────┐
                │        My Portfolio      │
                │       tickers.yaml       │
                └─────────────┬────────────┘
                              │
                              ▼
                ┌──────────────────────────┐
                │     ASX Announcements    │
                │      Data Retrieval      │
                └─────────────┬────────────┘
                              │
                              ▼
                ┌──────────────────────────┐
                │       Bob the Bot        │
                │      agent.py Engine     │
                └─────────────┬────────────┘
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼

   ┌───────────────┐  ┌────────────────┐  ┌────────────────┐
   │ Announcement   │  │  Document      │  │ AI Analysis    │
   │ Classification │  │  Retrieval     │  │ (OpenAI API)   │
   └───────┬───────┘  └───────┬────────┘  └────────┬───────┘
           │                  │                    │
           ▼                  ▼                    ▼

   ┌───────────────┐  ┌────────────────┐  ┌────────────────┐
   │ Portfolio     │  │ Google Drive   │  │ Strawman Post  │
   │ Intelligence  │  │ Document Store │  │ Draft Generator│
   └───────┬───────┘  └────────┬───────┘  └────────┬───────┘
           │                   │                   │
           └──────────────┬────┴─────────────┬─────┘
                          ▼                  ▼

                    ┌───────────────┐
                    │  Daily Email  │
                    │ Intelligence  │
                    │    Digest     │
                    └───────────────┘
What Bob Does

Every day Bob automatically:

Monitors ASX announcements for companies in my Portfolio

Identifies announcements released within the last 24 hours

Avoids reporting the same announcement on consecutive days by tracking seen announcement IDs between runs

Filters out routine filings and low-impact updates

Downloads PDFs for meaningful announcements

Extracts text from reports and presentations

Runs AI analysis on major announcements

Generates short summaries for quick review

Saves important documents to Google Drive

Sends a daily intelligence briefing email

Bob focuses on signal over noise.

Example Output

Bob sends a daily briefing structured like this:

Bob the Bot
Daily Announcements Digest — last 24 hours

HIGH IMPACT

DRO — FY Results

Revenue growth strong but margin compression continues.

Analysis:
[Detailed AI analysis]

Open:
https://www.asx.com.au/...

Drive:
https://drive.google.com/...

------------------------------------------------

MATERIAL

TWE: Retirement of Chief Financial Officer
So what: Leadership transition likely operational rather than strategic.

Open:
https://www.asx.com.au/...

------------------------------------------------

FYI

ABB: Appendix 3Y Director Interest Notice
So what: Routine filing.
Portfolio Monitoring

Companies are defined in:

tickers.yaml

Example:

asx:
  - DRO
  - AR9
  - CAT
  - ABB
  - TWE

Bob retrieves announcements for each ticker.

Announcement Source

Bob retrieves announcements directly from the ASX announcements feed.

Example endpoint:

https://www.asx.com.au/asx/v2/statistics/announcements.do?asxCode=DRO

Information extracted includes:

announcement title

release date

announcement URL

PDF document link

Only announcements from the last 24 hours are processed.

Announcement Classification

Announcements are automatically categorised:

Category	Description
RESULTS_HY_FY	Half year or full year results
ACQUISITION	M&A transactions
CAPITAL_OR_DEBT_RAISE	Equity issuance or refinancing
CONTRACT_MATERIAL	Major contracts or strategic updates
OTHER	Routine filings

Classification uses keyword detection and content signals.

Document Retrieval

For important announcements Bob retrieves the PDF.

Retrieval pipeline:

Step 1 — Direct Download

Using Python requests.

Step 2 — Browser Simulation

If blocked by the ASX consent page:

Playwright

simulates a browser to retrieve the file.

Step 3 — HTML Fallback

If the PDF cannot be retrieved, Bob extracts text from the webpage.

Text Extraction

When a PDF is downloaded:

pypdf

extracts the text.

Bob also checks that the content is meaningful to avoid analysing:

empty documents

ASX legal disclaimer pages

broken downloads

AI Analysis

Bob uses the OpenAI API.

Default model:

gpt-4o-mini

AI analysis is used selectively to control cost.

Deep Analysis Triggers

Detailed AI analysis runs only for major events:

Half-year results

Full-year results

Acquisitions

Capital raises

Debt refinancings

These produce:

• full AI analysis
• a Strawman-ready investor post
• saved documents in Google Drive

Quick Summaries

Most announcements receive a short two-line summary:

DRO: $21.7m Western Military Contracts
So what: Adds to defence pipeline but unlikely to materially move revenue this year.
Google Drive Storage

Important documents are automatically uploaded to Google Drive.

Stored documents include:

results reports

investor presentations

acquisition documents

capital raise materials

Drive links are included in the email.

Email Alerts

The daily digest contains:

HIGH IMPACT

Major announcements requiring deep analysis.

MATERIAL

Price-sensitive announcements requiring attention.

FYI

Routine announcements included for completeness.

SILENCE

If no announcements occurred:

No announcements in the last 24 hours.
Special Routing

Some tickers trigger additional alerts.

Example:

AR9

If AR9 appears in the announcements:

Bob sends a separate email digest to my brother.

Architecture

Core system files:

agent.py

Main orchestration engine.

playwright_fetch.py

Browser automation for retrieving ASX PDFs.

prompts.py

AI prompts used for announcement analysis.

tickers.yaml

Portfolio company list.

Workflow

Bob runs automatically using GitHub Actions.

Example schedule:

5 1 * * *

Which corresponds to:

09:05 Singapore Time

Bob runs entirely in the GitHub cloud environment.

Your computer does not need to be on.

Required Environment Variables

Configured using GitHub Secrets:

EMAIL_FROM
EMAIL_TO
EMAIL_APP_PASSWORD
OPENAI_API_KEY
GDRIVE_SERVICE_ACCOUNT_JSON
GDRIVE_FOLDER_ID
BROTHER_EMAIL
Safety Limits

To prevent runaway API usage:

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

Bob runs fully in the cloud via:

GitHub Actions

Execution environment:

Python 3.11
GitHub Hosted Runner
Future Improvements

Potential upgrades:

LSE announcement monitoring

broker research ingestion

earnings model extraction

insider trading detection

Slack or Telegram alerts

portfolio dashboard visualisation

Why Bob Exists

The ASX produces hundreds of announcements every week.

Most are irrelevant.

Bob's job is to read everything so I don't have to, highlight the important information, and deliver a clean intelligence briefing for my Portfolio.
