package eventhub

import (
	"context"
	"fmt"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/eventhub/armeventhub"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/resources/armresources"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	rgClient         *armresources.ResourceGroupsClient
	namespacesClient *armeventhub.NamespacesClient
	hubsClient       *armeventhub.EventHubsClient
	cfg              config.Config
}

func New(cred azcore.TokenCredential, cfg config.Config) (*Cleaner, error) {
	rgClient, err := armresources.NewResourceGroupsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure resource groups client: %w", err)
	}
	namespacesClient, err := armeventhub.NewNamespacesClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure eventhub namespaces client: %w", err)
	}
	hubsClient, err := armeventhub.NewEventHubsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure eventhub client: %w", err)
	}

	return &Cleaner{
		rgClient:         rgClient,
		namespacesClient: namespacesClient,
		hubsClient:       hubsClient,
		cfg:              cfg,
	}, nil
}

func (c *Cleaner) Name() string {
	return "azure-eventhub"
}

func (c *Cleaner) Run(ctx context.Context) core.Result {
	result := core.Result{Name: c.Name()}
	cutoff := time.Now().Add(-c.cfg.MaxAge)

	rgPager := c.rgClient.NewListPager(nil)
	for rgPager.More() {
		rgPage, err := rgPager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing resource groups failed: %v\n", c.Name(), err)
			result.Errors++
			break
		}

		for _, rg := range rgPage.Value {
			rgName := str(rg.Name)
			if rgName == "" {
				continue
			}

			nsPager := c.namespacesClient.NewListByResourceGroupPager(rgName, nil)
			for nsPager.More() {
				nsPage, err := nsPager.NextPage(ctx)
				if err != nil {
					fmt.Printf("[%s] listing eventhub namespaces failed in resource group %s: %v\n", c.Name(), rgName, err)
					result.Errors++
					break
				}

				for _, ns := range nsPage.Value {
					nsName := str(ns.Name)
					if nsName == "" {
						continue
					}

					hubPager := c.hubsClient.NewListByNamespacePager(rgName, nsName, nil)
					for hubPager.More() {
						hubPage, err := hubPager.NextPage(ctx)
						if err != nil {
							fmt.Printf("[%s] listing eventhubs failed in %s/%s: %v\n", c.Name(), rgName, nsName, err)
							result.Errors++
							break
						}

						for _, hub := range hubPage.Value {
							result.Found++
							name := str(hub.Name)
							if name == "" {
								continue
							}

							createdAt := hubCreatedAt(hub)
							if createdAt == nil || createdAt.After(cutoff) {
								continue
							}

							if c.cfg.DryRun {
								fmt.Printf("[%s][dry-run] would delete eventhub %s/%s/%s (created %s)\n", c.Name(), rgName, nsName, name, createdAt.UTC().Format(time.RFC3339))
								result.Deleted++
								continue
							}

							_, err := c.hubsClient.Delete(ctx, rgName, nsName, name, nil)
							if err != nil {
								fmt.Printf("[%s] delete failed for eventhub %s/%s/%s: %v\n", c.Name(), rgName, nsName, name, err)
								result.Errors++
								continue
							}

							fmt.Printf("[%s] deleted eventhub %s/%s/%s\n", c.Name(), rgName, nsName, name)
							result.Deleted++
						}
					}
				}
			}
		}
	}

	return result
}

func hubCreatedAt(hub *armeventhub.Eventhub) *time.Time {
	if hub == nil || hub.Properties == nil {
		return nil
	}
	return hub.Properties.CreatedAt
}

func str(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}
