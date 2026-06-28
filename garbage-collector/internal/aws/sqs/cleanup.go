package sqs

import (
	"context"
	"fmt"
	"strconv"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/sqs/types"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	client *sqs.Client
	dryRun bool
	maxAge time.Duration
}

func New(awsCfg awssdk.Config, dryRun bool, maxAge time.Duration) *Cleaner {
	return &Cleaner{client: sqs.NewFromConfig(awsCfg), dryRun: dryRun, maxAge: maxAge}
}

func (c *Cleaner) Name() string {
	return "aws-sqs"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name(), DryRun: c.dryRun}
	cutoff := time.Now().Add(-c.maxAge)

	pager := sqs.NewListQueuesPaginator(c.client, &sqs.ListQueuesInput{})
	for pager.HasMorePages() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing queues failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, queueURL := range page.QueueUrls {
			result.Found++
			createdAt, err := c.queueCreatedAt(ctx, queueURL)
			if err != nil {
				fmt.Printf("[%s] reading queue attributes failed for %s: %v\n", c.Name(), queueURL, err)
				result.Errors++
				continue
			}

			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.dryRun {
				fmt.Printf("[%s][dry-run] would delete queue %s (created %s)\n", c.Name(), queueURL, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err = c.client.DeleteQueue(ctx, &sqs.DeleteQueueInput{QueueUrl: awssdk.String(queueURL)})
			if err != nil {
				fmt.Printf("[%s] delete failed for queue %s: %v\n", c.Name(), queueURL, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted queue %s\n", c.Name(), queueURL)
			result.Deleted++
		}
	}

	return result
}

func (c *Cleaner) queueCreatedAt(ctx context.Context, queueURL string) (*time.Time, error) {
	attrsOutput, err := c.client.GetQueueAttributes(ctx, &sqs.GetQueueAttributesInput{
		QueueUrl:       awssdk.String(queueURL),
		AttributeNames: []types.QueueAttributeName{types.QueueAttributeNameCreatedTimestamp},
	})
	if err != nil {
		return nil, err
	}

	timestampText, ok := attrsOutput.Attributes[string(types.QueueAttributeNameCreatedTimestamp)]
	if !ok || timestampText == "" {
		return nil, nil
	}

	epoch, err := strconv.ParseInt(timestampText, 10, 64)
	if err != nil {
		return nil, fmt.Errorf("parsing CreatedTimestamp %q: %w", timestampText, err)
	}

	t := time.Unix(epoch, 0).UTC()
	return &t, nil
}
