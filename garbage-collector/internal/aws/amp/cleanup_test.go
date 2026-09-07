package amp

import (
	"context"
	"testing"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	awssvc "github.com/aws/aws-sdk-go-v2/service/amp"
	"github.com/aws/aws-sdk-go-v2/service/amp/types"
)

type fakeClient struct {
	workspaces []types.WorkspaceSummary
	deleted    []string
}

func (f *fakeClient) ListWorkspaces(context.Context, *awssvc.ListWorkspacesInput, ...func(*awssvc.Options)) (*awssvc.ListWorkspacesOutput, error) {
	return &awssvc.ListWorkspacesOutput{Workspaces: f.workspaces}, nil
}

func (f *fakeClient) DeleteWorkspace(_ context.Context, input *awssvc.DeleteWorkspaceInput, _ ...func(*awssvc.Options)) (*awssvc.DeleteWorkspaceOutput, error) {
	f.deleted = append(f.deleted, awssdk.ToString(input.WorkspaceId))
	return &awssvc.DeleteWorkspaceOutput{}, nil
}

func TestRunDeletesOnlyStaleE2EWorkspaces(t *testing.T) {
	now := time.Now()
	client := &fakeClient{workspaces: []types.WorkspaceSummary{
		workspace("stale", workspaceAliasPrefix+"stale", now.Add(-24*time.Hour), types.WorkspaceStatusCodeActive),
		workspace("young", workspaceAliasPrefix+"young", now.Add(-time.Hour), types.WorkspaceStatusCodeActive),
		workspace("unowned", "production", now.Add(-24*time.Hour), types.WorkspaceStatusCodeActive),
		workspace("deleting", workspaceAliasPrefix+"deleting", now.Add(-24*time.Hour), types.WorkspaceStatusCodeDeleting),
	}}
	cleaner := &Cleaner{client: client, maxAge: 12 * time.Hour}

	result := cleaner.Run(context.Background())

	if result.Found != 3 || result.Deleted != 1 || result.Errors != 0 {
		t.Fatalf("unexpected result: %+v", result)
	}
	if len(client.deleted) != 1 || client.deleted[0] != "stale" {
		t.Fatalf("deleted workspaces = %v, want [stale]", client.deleted)
	}
}

func TestRunDryRunDoesNotDelete(t *testing.T) {
	client := &fakeClient{workspaces: []types.WorkspaceSummary{
		workspace("stale", workspaceAliasPrefix+"stale", time.Now().Add(-24*time.Hour), types.WorkspaceStatusCodeActive),
	}}
	cleaner := &Cleaner{client: client, dryRun: true, maxAge: 12 * time.Hour}

	result := cleaner.Run(context.Background())

	if result.Deleted != 1 {
		t.Fatalf("deleted count = %d, want 1", result.Deleted)
	}
	if len(client.deleted) != 0 {
		t.Fatalf("dry run deleted workspaces: %v", client.deleted)
	}
}

func workspace(id, alias string, createdAt time.Time, status types.WorkspaceStatusCode) types.WorkspaceSummary {
	return types.WorkspaceSummary{
		Alias:       awssdk.String(alias),
		CreatedAt:   awssdk.Time(createdAt),
		Status:      &types.WorkspaceStatus{StatusCode: status},
		WorkspaceId: awssdk.String(id),
	}
}
