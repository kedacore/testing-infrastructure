package core

import (
	"context"
	"fmt"
)

func RunAll(ctx context.Context, cleaners []Cleaner) int {
	totalErrors := 0
	fmt.Println("Starting garbage collector run")
	for _, cleaner := range cleaners {
		result := cleaner.Run(ctx)
		totalErrors += result.Errors
		fmt.Printf("[%s] found=%d deleted=%d errors=%d\n", result.Name, result.Found, result.Deleted, result.Errors)
	}

	if totalErrors > 0 {
		fmt.Printf("Finished with errors: %d\n", totalErrors)
		return 1
	}

	fmt.Println("Finished successfully")
	return 0
}
