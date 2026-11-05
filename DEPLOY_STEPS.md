# Deploy BrandGuard to Render (free)

1. Create a GitHub repo (e.g. `brandguard`) and upload everything inside this folder (the files, not the folder itself).
2. Go to https://render.com, sign in with GitHub, New + > Web Service, pick the repo.
3. Render reads render.yaml. If it asks: Runtime Python, Build `pip install -r requirements.txt`,
   Start `gunicorn app:app --workers 1 --threads 4 --timeout 180 --bind 0.0.0.0:$PORT`, Plan Free.
4. Click Deploy. After 2-5 minutes you get a link like https://brandguard-xxxx.onrender.com
5. Free plan sleeps after ~15 min idle: open the link 2 minutes before presenting.
6. Keep workers = 1 (the app keeps state in memory). Data is synthetic; sending is simulated.
