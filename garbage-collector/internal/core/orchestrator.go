package core

import (
	"context"
	"fmt"
)

func RunAll(ctx context.Context, cleaners []Cleaner) int {
	totalFound := 0
	totalDeleted := 0
	totalErrors := 0
	results := make([]Result, 0, len(cleaners))
	dryRunMode := false
	fmt.Println("Starting garbage collector run")
	for _, cleaner := range cleaners {
		result := cleaner.Run(ctx)
		results = append(results, result)
		totalFound += result.Found
		totalDeleted += result.Deleted
		totalErrors += result.Errors
		dryRunMode = dryRunMode || result.DryRun
	}

	fmt.Println("Summary:")
	deletedLabel := "deleted"
	if dryRunMode {
		deletedLabel = "would_delete"
	}
	for _, result := range results {
		fmt.Printf("[%s] found=%d %s=%d errors=%d\n", result.Name, result.Found, deletedLabel, result.Deleted, result.Errors)
	}
	fmt.Printf("total_found=%d total_%s=%d total_errors=%d\n", totalFound, deletedLabel, totalDeleted, totalErrors)

	if totalErrors > 0 {
		fmt.Printf("Finished with errors: %d\n", totalErrors)
		return 1
	}

	fmt.Println("Finished successfully")
	return 0
}
