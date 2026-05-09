package dashboard

import (
	"strings"
	"unicode"
)

func RunBucketCounts(rows []TraceSummary) BucketCounts {
	counts := BucketCounts{
		BucketActive:     0,
		BucketAttention:  0,
		BucketRecent:     0,
		BucketSuccessful: 0,
		BucketFailed:     0,
		BucketHidden:     0,
	}
	for _, row := range rows {
		if IsHiddenRun(row) {
			counts[BucketHidden]++
			continue
		}
		counts[BucketRecent]++
		if IsActiveRun(row) {
			counts[BucketActive]++
		}
		if NeedsAttention(row) {
			counts[BucketAttention]++
		}
		if IsSuccessfulRun(row) {
			counts[BucketSuccessful]++
		}
		if IsFailedRun(row) {
			counts[BucketFailed]++
		}
	}
	return counts
}

func FilterRuns(rows []TraceSummary, bucket RunBucket) []TraceSummary {
	out := make([]TraceSummary, 0, len(rows))
	for _, row := range rows {
		switch bucket {
		case BucketActive:
			if !IsHiddenRun(row) && IsActiveRun(row) {
				out = append(out, row)
			}
		case BucketAttention:
			if !IsHiddenRun(row) && NeedsAttention(row) {
				out = append(out, row)
			}
		case BucketSuccessful:
			if !IsHiddenRun(row) && IsSuccessfulRun(row) {
				out = append(out, row)
			}
		case BucketFailed:
			if !IsHiddenRun(row) && IsFailedRun(row) {
				out = append(out, row)
			}
		case BucketHidden:
			if IsHiddenRun(row) {
				out = append(out, row)
			}
		default:
			if !IsHiddenRun(row) {
				out = append(out, row)
			}
		}
	}
	return out
}

func RunOutcomeLabel(row TraceSummary) string {
	if IsHiddenRun(row) {
		return "Hidden"
	}
	if IsActiveRun(row) {
		if row.Status == "queued" {
			return "Queued"
		}
		return "Active"
	}
	if IsFailedRun(row) {
		return "Failed"
	}
	if NeedsAttention(row) {
		return "Needs attention"
	}
	if IsSuccessfulRun(row) {
		return "Successful"
	}
	return titleCase(firstNonEmpty(row.RawStatus, row.Status, "Run"))
}

func IsLiveRun(row TraceSummary) bool {
	return !IsHiddenRun(row) && IsActiveRun(row)
}

func IsHiddenRun(row TraceSummary) bool {
	return row.Hidden || row.Status == "hidden"
}

func IsActiveRun(row TraceSummary) bool {
	return row.Status == "in-flight" || row.Status == "queued"
}

func NeedsAttention(row TraceSummary) bool {
	if row.Status == "stuck" {
		return true
	}
	if row.LifecycleState == "stale" {
		return true
	}
	if IsFailedRun(row) {
		return true
	}
	return normalizedRaw(row) == "submitted" && strings.Contains(row.Elapsed, ">24h")
}

func IsSuccessfulRun(row TraceSummary) bool {
	if IsFailedRun(row) {
		return false
	}
	raw := normalizedRaw(row)
	return row.Status == "done" && (raw == "complete" || raw == "completed" || raw == "pr created" || raw == "merged" || raw == "closed")
}

func IsFailedRun(row TraceSummary) bool {
	raw := normalizedRaw(row)
	return strings.Contains(raw, "fail") ||
		strings.Contains(raw, "skip") ||
		strings.Contains(raw, "escalat") ||
		strings.Contains(raw, "error")
}

func normalizedRaw(row TraceSummary) string {
	return strings.ToLower(strings.TrimSpace(firstNonEmpty(row.RawStatus, row.Status)))
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func titleCase(value string) string {
	parts := strings.FieldsFunc(value, func(r rune) bool {
		return unicode.IsSpace(r) || r == '_' || r == '-'
	})
	for i, part := range parts {
		if part == "" {
			continue
		}
		lower := strings.ToLower(part)
		runes := []rune(lower)
		runes[0] = unicode.ToUpper(runes[0])
		parts[i] = string(runes)
	}
	return strings.Join(parts, " ")
}
