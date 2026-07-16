# Rollback Plan

Document how to roll back each deployed component. Due diligence teams check for an "exercised rollback story."

## Frontend (Vercel)
- Vercel dashboard → Deployments → click previous deployment → Promote to Production
- Instant rollback, zero downtime
- Test: do a test rollback after first production deploy

## Backend Workers (Railway)
- Railway dashboard → select service → Deployments → Rollback
- Or: `git revert <commit> && git push` triggers automatic redeploy
- Test: do a test rollback after first production deploy

## Scrapers (VPS)
- SSH into VPS, `cd /path/to/scrapers`
- `git log --oneline` to find last good commit
- `git checkout <commit>` and restart cron
- Or: restore from VPS provider snapshot

## Firestore Security Rules
- Firebase Console → Firestore → Rules → scroll to version history → restore previous version
- CRITICAL: never deploy less-restrictive rules without a rollback plan
- Keep a copy of production rules in `firebase/firestore.rules` in the repo

## Database (Firestore)
- Firestore has no built-in point-in-time restore for Spark/Blaze plans
- Mitigation: export critical collections before risky migrations using `gcloud firestore export`
- For V1: accept that Firestore rollback is manual. Add automated backups as a Phase 2+ improvement.

## Status
- [ ] Frontend rollback tested
- [ ] Backend rollback tested
- [ ] VPS rollback tested
- [ ] Firestore rules rollback tested
