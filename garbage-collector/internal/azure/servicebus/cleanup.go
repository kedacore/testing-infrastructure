package servicebus

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/resources/armresources"
	"github.com/Azure/azure-sdk-for-go/sdk/resourcemanager/servicebus/armservicebus"

	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/config"
	"github.com/kedacore/testing-infrastructure/garbage-colletor/internal/core"
)

type Cleaner struct {
	rgClient         *armresources.ResourceGroupsClient
	namespacesClient *armservicebus.NamespacesClient
	queuesClient     *armservicebus.QueuesClient
	topicsClient     *armservicebus.TopicsClient
	cfg              config.Config
}

func New(cred azcore.TokenCredential, cfg config.Config) (*Cleaner, error) {
	rgClient, err := armresources.NewResourceGroupsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure resource groups client: %w", err)
	}
	namespacesClient, err := armservicebus.NewNamespacesClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure servicebus namespaces client: %w", err)
	}
	queuesClient, err := armservicebus.NewQueuesClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure servicebus queues client: %w", err)
	}
	topicsClient, err := armservicebus.NewTopicsClient(cfg.AzureSubscriptionID, cred, nil)
	if err != nil {
		return nil, fmt.Errorf("creating azure servicebus topics client: %w", err)
	}

	return &Cleaner{
		rgClient:         rgClient,
		namespacesClient: namespacesClient,
		queuesClient:     queuesClient,
		topicsClient:     topicsClient,
		cfg:              cfg,
	}, nil
}

func (c *Cleaner) Name() string {
	return "azure-servicebus"
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
					fmt.Printf("[%s] listing servicebus namespaces failed in resource group %s: %v\n", c.Name(), rgName, err)
					result.Errors++
					break
				}

				for _, ns := range nsPage.Value {
					namespace := str(ns.Name)
					if namespace == "" {
						continue
					}

					c.cleanupQueues(ctx, rgName, namespace, cutoff, &result)
					c.cleanupTopics(ctx, rgName, namespace, cutoff, &result)
				}
			}
		}
	}

	return result
}

func (c *Cleaner) cleanupQueues(ctx context.Context, resourceGroup, namespace string, cutoff time.Time, result *core.Result) {
	pager := c.queuesClient.NewListByNamespacePager(resourceGroup, namespace, nil)
	for pager.More() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing queues failed in %s/%s: %v\n", c.Name(), resourceGroup, namespace, err)
			result.Errors++
			break
		}

		for _, queue := range page.Value {
			result.Found++
			name := str(queue.Name)
			if name == "" {
				continue
			}

			createdAt := queueCreatedAt(queue)
			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.cfg.DryRun {
				fmt.Printf("[%s][dry-run] would delete queue %s/%s/%s (created %s)\n", c.Name(), resourceGroup, namespace, name, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err := c.queuesClient.Delete(ctx, resourceGroup, namespace, name, nil)
			if err != nil {
				fmt.Printf("[%s] delete failed for queue %s/%s/%s: %v\n", c.Name(), resourceGroup, namespace, name, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted queue %s/%s/%s\n", c.Name(), resourceGroup, namespace, name)
			result.Deleted++
		}
	}
}

func (c *Cleaner) cleanupTopics(ctx context.Context, resourceGroup, namespace string, cutoff time.Time, result *core.Result) {
	pager := c.topicsClient.NewListByNamespacePager(resourceGroup, namespace, nil)
	for pager.More() {
		page, err := pager.NextPage(ctx)
		if err != nil {
			fmt.Printf("[%s] listing topics failed in %s/%s: %v\n", c.Name(), resourceGroup, namespace, err)
			result.Errors++
			break
		}

		for _, topic := range page.Value {
			result.Found++
			name := str(topic.Name)
			if name == "" {
				continue
			}

			if c.isExcludedTopic(name) {
				continue
			}

			createdAt := topicCreatedAt(topic)
			if createdAt == nil || createdAt.After(cutoff) {
				continue
			}

			if c.cfg.DryRun {
				fmt.Printf("[%s][dry-run] would delete topic %s/%s/%s (created %s)\n", c.Name(), resourceGroup, namespace, name, createdAt.UTC().Format(time.RFC3339))
				result.Deleted++
				continue
			}

			_, err := c.topicsClient.Delete(ctx, resourceGroup, namespace, name, nil)
			if err != nil {
				fmt.Printf("[%s] delete failed for topic %s/%s/%s: %v\n", c.Name(), resourceGroup, namespace, name, err)
				result.Errors++
				continue
			}

			fmt.Printf("[%s] deleted topic %s/%s/%s\n", c.Name(), resourceGroup, namespace, name)
			result.Deleted++
		}
	}
}

func queueCreatedAt(queue *armservicebus.SBQueue) *time.Time {
	if queue == nil || queue.Properties == nil {
		return nil
	}
	return queue.Properties.CreatedAt
}

func topicCreatedAt(topic *armservicebus.SBTopic) *time.Time {
	if topic == nil || topic.Properties == nil {
		return nil
	}
	return topic.Properties.CreatedAt
}

func str(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

func (c *Cleaner) isExcludedTopic(name string) bool {
	for _, suffix := range c.cfg.ExcludedSBTopicSuffixes {
		if strings.HasSuffix(name, suffix) {
			return true
		}
	}
	return false
}
