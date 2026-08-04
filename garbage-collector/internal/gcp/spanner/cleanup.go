package spanner

import (
	"context"
	"fmt"
	"strings"
	"time"

	instance "cloud.google.com/go/spanner/admin/instance/apiv1"
	"cloud.google.com/go/spanner/admin/instance/apiv1/instancepb"
	"google.golang.org/api/iterator"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

// e2eSpannerInstancePrefix matches instances created by keda e2e tests - https://github.com/kedacore/keda/pull/7844
const e2eSpannerInstancePrefix = "keda-e2e-spanner-"

type Cleaner struct {
	client *instance.InstanceAdminClient
	cfg    config.Config
}

func New(ctx context.Context, cfg config.Config) (*Cleaner, error) {
	client, err := instance.NewInstanceAdminClient(ctx)
	if err != nil {
		return nil, fmt.Errorf("creating gcp spanner instance admin client: %w", err)
	}
	return &Cleaner{client: client, cfg: cfg}, nil
}

func (c *Cleaner) Name() string {
	return "gcp-spanner"
}

func (c *Cleaner) Close() {
	if err := c.client.Close(); err != nil {
		fmt.Printf("[%s] closing client failed: %v\n", c.Name(), err)
	}
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name(), DryRun: c.cfg.DryRun}
	cutoff := time.Now().Add(-c.cfg.MaxAge)

	it := c.client.ListInstances(ctx, &instancepb.ListInstancesRequest{
		Parent: fmt.Sprintf("projects/%s", c.cfg.GCPProjectID),
	})
	for {
		inst, err := it.Next()
		if err == iterator.Done {
			break
		}
		if err != nil {
			fmt.Printf("[%s] listing instances failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		instanceID := inst.GetName()[strings.LastIndex(inst.GetName(), "/")+1:]
		if !strings.HasPrefix(instanceID, e2eSpannerInstancePrefix) {
			continue
		}
		result.Found++

		createdAt := instanceCreatedAt(inst)
		if createdAt == nil || createdAt.After(cutoff) {
			continue
		}

		if c.cfg.DryRun {
			fmt.Printf("[%s][dry-run] would delete instance %s (created %s)\n", c.Name(), instanceID, createdAt.UTC().Format(time.RFC3339))
			result.Deleted++
			continue
		}

		// DeleteInstance also drops all databases hosted on the instance.
		err = c.client.DeleteInstance(ctx, &instancepb.DeleteInstanceRequest{Name: inst.GetName()})
		if err != nil {
			fmt.Printf("[%s] delete failed for instance %s: %v\n", c.Name(), instanceID, err)
			result.Errors++
			continue
		}

		fmt.Printf("[%s] deleted instance %s\n", c.Name(), instanceID)
		result.Deleted++
	}

	return result
}

func instanceCreatedAt(inst *instancepb.Instance) *time.Time {
	if ts := inst.GetCreateTime(); ts != nil {
		t := ts.AsTime()
		return &t
	}

	// Fallback: e2e instances embed a UnixNano timestamp as the name suffix.
	suffix := inst.GetName()[strings.LastIndex(inst.GetName(), "-")+1:]
	var nanos int64
	if _, err := fmt.Sscanf(suffix, "%d", &nanos); err != nil || nanos <= 0 {
		return nil
	}
	t := time.Unix(0, nanos).UTC()
	return &t
}
