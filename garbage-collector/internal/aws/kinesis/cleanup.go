package kinesis

import (
	"context"
	"fmt"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/kinesis"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	client *kinesis.Client
	dryRun bool
	maxAge time.Duration
}

func New(awsCfg awssdk.Config, dryRun bool, maxAge time.Duration) *Cleaner {
	return &Cleaner{client: kinesis.NewFromConfig(awsCfg), dryRun: dryRun, maxAge: maxAge}
}

func (c *Cleaner) Name() string {
	return "aws-kinesis"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name()}
	cutoff := time.Now().Add(-c.maxAge)

	pager := kinesis.NewListStreamsPaginator(c.client, &kinesis.ListStreamsInput{})
	for pager.HasMorePages() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing streams failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, streamName := range page.StreamNames {
			result.Found++
			desc, err := c.client.DescribeStreamSummary(ctx, &kinesis.DescribeStreamSummaryInput{
				StreamName: awssdk.String(streamName),
			})
			if err != nil {
				fmt.Printf("[%s] describe stream failed for %s: %v\n", c.Name(), streamName, err)
				result.Errors++
				continue
			}

			createdAt := desc.StreamDescriptionSummary.StreamCreationTimestamp
			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.dryRun {
				fmt.Printf("[%s][dry-run] would delete stream %s (created %s)\n", c.Name(), streamName, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err = c.client.DeleteStream(ctx, &kinesis.DeleteStreamInput{
				StreamName:              awssdk.String(streamName),
				EnforceConsumerDeletion: awssdk.Bool(true),
			})
			if err != nil {
				fmt.Printf("[%s] delete stream failed for %s: %v\n", c.Name(), streamName, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted stream %s\n", c.Name(), streamName)
			result.Deleted++
		}
	}

	return result
}
