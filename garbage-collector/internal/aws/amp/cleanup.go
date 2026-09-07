package amp

import (
	"context"
	"fmt"
	"strings"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/amp"
	"github.com/aws/aws-sdk-go-v2/service/amp/types"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

const workspaceAliasPrefix = "keda-e2e-amp-"

type apiClient interface {
	ListWorkspaces(context.Context, *amp.ListWorkspacesInput, ...func(*amp.Options)) (*amp.ListWorkspacesOutput, error)
	DeleteWorkspace(context.Context, *amp.DeleteWorkspaceInput, ...func(*amp.Options)) (*amp.DeleteWorkspaceOutput, error)
}

type Cleaner struct {
	client apiClient
	dryRun bool
	maxAge time.Duration
}

func New(awsCfg awssdk.Config, dryRun bool, maxAge time.Duration) *Cleaner {
	return &Cleaner{client: amp.NewFromConfig(awsCfg), dryRun: dryRun, maxAge: maxAge}
}

func (c *Cleaner) Name() string {
	return "aws-amp"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name(), DryRun: c.dryRun}
	cutoff := time.Now().Add(-c.maxAge)

	pager := amp.NewListWorkspacesPaginator(c.client, &amp.ListWorkspacesInput{})
	for pager.HasMorePages() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing workspaces failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, workspace := range page.Workspaces {
			if workspace.Alias == nil || !strings.HasPrefix(*workspace.Alias, workspaceAliasPrefix) {
				continue
			}
			result.Found++
			if workspace.CreatedAt == nil || workspace.CreatedAt.After(cutoff) {
				continue
			}
			if workspace.WorkspaceId == nil || workspace.Status == nil {
				fmt.Printf("[%s] stale workspace has incomplete metadata\n", c.Name())
				result.Errors++
				continue
			}
			if workspace.Status.StatusCode == types.WorkspaceStatusCodeDeleting {
				fmt.Printf("[%s] workspace %s is already being deleted\n", c.Name(), *workspace.WorkspaceId)
				continue
			}
			if workspace.Status.StatusCode != types.WorkspaceStatusCodeActive {
				fmt.Printf("[%s] cannot delete stale workspace %s in status %s\n", c.Name(), *workspace.WorkspaceId, workspace.Status.StatusCode)
				result.Errors++
				continue
			}

			if c.dryRun {
				fmt.Printf("[%s][dry-run] would delete workspace %s (created %s)\n", c.Name(), *workspace.WorkspaceId, workspace.CreatedAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err = c.client.DeleteWorkspace(ctx, &amp.DeleteWorkspaceInput{WorkspaceId: workspace.WorkspaceId})
			if err != nil {
				fmt.Printf("[%s] delete workspace failed for %s: %v\n", c.Name(), *workspace.WorkspaceId, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted workspace %s\n", c.Name(), *workspace.WorkspaceId)
			result.Deleted++
		}
	}

	return result
}
