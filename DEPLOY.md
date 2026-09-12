# 🚀 Deploy ResearchLens AI to Render (Free, 24/7)

## Overview
Render hosts your Flask app for free so it runs 24/7 without your laptop being on.
The daily ArXiv check and real-time tracking will work continuously after deployment.

---

## STEP 1 — Push your project to GitHub

### Install Git (if not already)
Download from: https://git-scm.com/download/win

### Create a GitHub account
Go to: https://github.com and sign up (free)

### Push your project
Open VS Code terminal and run these commands ONE BY ONE:

```
cd C:\Users\megha\OneDrive\Desktop\research_gap_detector\research_gap_detector

git init
git add .
git commit -m "Initial ResearchLens AI commit"
```

Then go to https://github.com/new and create a new repository:
- Name: `researchlens-ai`
- Visibility: Public (required for Render free tier)
- Do NOT initialize with README (you already have files)

Copy the two commands GitHub shows you after creating the repo, they look like:
```
git remote add origin https://github.com/YOUR_USERNAME/researchlens-ai.git
git push -u origin main
```

Run those in your terminal.

---

## STEP 2 — Deploy on Render

1. Go to: https://render.com and sign up with GitHub

2. Click **"New +"** → **"Web Service"**

3. Connect your GitHub account if prompted

4. Select your `researchlens-ai` repository

5. Render will auto-detect the `render.yaml` file — it will pre-fill:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn --chdir backend app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`

6. Scroll down to **Environment Variables** and add:
   ```
   FLASK_DEBUG = false
   ```

7. Click **"Create Web Service"**

8. Wait ~3-5 minutes for the first build to complete

9. Render gives you a URL like: `https://researchlens-ai.onrender.com`
   That's your live app — share it with anyone!

---

## STEP 3 — Important Notes About Free Tier

**Free tier spins down after 15 minutes of inactivity.**
This means the first visit after idle will take ~30 seconds to wake up.
For your presentation, just open the URL a minute before you demo — it stays awake while there's traffic.

**Persistent storage:**
On the free tier, uploaded PDFs and generated reports are stored in `/tmp` which resets on each deploy. Your SQLite database (tracked topics, analysis history) is also in `/tmp` — this means tracked topics reset when the app redeploys. For permanent storage you'd need Render's paid disk option or an external DB like Supabase (free tier available).

**Daily scheduler:**
The APScheduler will run as long as your Render instance is awake. On the free tier, it may miss some scheduled 24-hour checks if the instance is sleeping. The "Check Now" button in the profile panel always works on demand regardless.

---

## STEP 4 — Update after changes

Every time you make changes locally:
```
git add .
git commit -m "describe what you changed"
git push
```

Render automatically redeploys within ~2 minutes of a push.

---

## Troubleshooting

**Build fails:**
Check the Render build logs — usually a missing package in requirements.txt

**App crashes on start:**
Check Render logs — usually an import error or missing env variable

**ArXiv still timing out:**
Render's servers have reliable internet — the ArXiv timeouts you saw locally
(which were your network/firewall) should not happen on Render's infrastructure.