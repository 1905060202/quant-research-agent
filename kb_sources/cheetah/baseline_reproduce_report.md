# Baseline Reproduction Report

**Generated**: 2026-05-26 00:24:26
**Script**: features_v10_reproduce.py (exact replica of features_v10.py)
**Elapsed**: 10.5 min

## Key Parameters

| Parameter | Value |
|-----------|-------|
| Random Seed | 42 |
| BLIND Date | 2026-04-01 |
| Folds (NF) | 5 |
| Purge Gap (PG) | 30 |
| Horizon | 20d |
| Feature Cols | 172 |
| Rows | 2,526,746 |
| Unique Symbols | 5176 |

## CV Results (by Tier)

| Tier | CV Mean IC | Folds | Features |
|------|-----------|-------|----------|
| large | 0.0333 | 3 | 171 |
| mid | 0.1244 | 3 | 171 |
| small | 0.0968 | 3 | 171 |

## Blind IC Results

| Tier | 5d IC | 20d IC | Stocks |
|------|-------|--------|--------|
| large | 0.0249 | 0.0156 | 66 |
| mid | 0.1114 | 0.1658 | 66 |
| small | -0.0561 | -0.1008 | 5044 |
| **Overall (weighted)** | **-0.0529** | **-0.0959** | 5176 |

## IC Comparison: Reproduction vs Production

| Metric | Reproduced | Production | Delta | Within Tolerance? |
|--------|-----------|------------|-------|-------------------|
| Overall 5d IC | -0.0529 | 0.1260 | -0.1789 | NO (gap > 0.005) |
| Overall 20d IC | -0.0959 | 0.126 (benchmark) | -0.2219 | NO (gap > 0.005) |

## Delta from v9 Baseline (IC=0.0868)

| Metric | Value |
|--------|-------|
| v9 Baseline 5d IC | 0.0868 |
| v10 Reproduced 5d IC | -0.0529 |
| Improvement vs v9 | -0.1397 |

## Training Configuration

### LGB Hyperparameters (tier-specific)

**large**: n_estimators=400, max_depth=6, num_leaves=63, learning_rate=0.03, min_child_samples=50, subsample=0.8, colsample_bytree=0.7

**mid**: n_estimators=500, max_depth=7, num_leaves=127, learning_rate=0.025, min_child_samples=30, subsample=0.75, colsample_bytree=0.65

**small**: n_estimators=700, max_depth=8, num_leaves=200, learning_rate=0.02, min_child_samples=20, subsample=0.7, colsample_bytree=0.6

## Conclusion

:warning: **REPRODUCTION GAP**. Reproduced IC (-0.0529) differs from production IC (0.126) by -0.1789 (>0.005 threshold).
Possible causes: data drift, random seed sensitivity, environment differences (OS/LGB version).
