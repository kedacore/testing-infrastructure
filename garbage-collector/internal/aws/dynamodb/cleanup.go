package dynamodb

import (
	"context"
	"fmt"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	client *dynamodb.Client
	dryRun bool
	maxAge time.Duration
}

func New(awsCfg awssdk.Config, dryRun bool, maxAge time.Duration) *Cleaner {
	return &Cleaner{client: dynamodb.NewFromConfig(awsCfg), dryRun: dryRun, maxAge: maxAge}
}

func (c *Cleaner) Name() string {
	return "aws-dynamodb"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name(), DryRun: c.dryRun}
	cutoff := time.Now().Add(-c.maxAge)

	pager := dynamodb.NewListTablesPaginator(c.client, &dynamodb.ListTablesInput{})
	for pager.HasMorePages() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing tables failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, tableName := range page.TableNames {
			result.Found++
			desc, err := c.client.DescribeTable(ctx, &dynamodb.DescribeTableInput{TableName: awssdk.String(tableName)})
			if err != nil {
				fmt.Printf("[%s] describe table failed for %s: %v\n", c.Name(), tableName, err)
				result.Errors++
				continue
			}

			createdAt := desc.Table.CreationDateTime
			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.dryRun {
				fmt.Printf("[%s][dry-run] would delete table %s (created %s)\n", c.Name(), tableName, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err = c.client.DeleteTable(ctx, &dynamodb.DeleteTableInput{TableName: awssdk.String(tableName)})
			if err != nil {
				fmt.Printf("[%s] delete table failed for %s: %v\n", c.Name(), tableName, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted table %s\n", c.Name(), tableName)
			result.Deleted++
		}
	}

	return result
}
