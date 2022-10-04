# KEDA Testing infrastructure

This repository is used to generated all the infrastucture for e2e tests in kedacore.

The infrastucture is divided in different modules grouped by cloud provider, which generate as outputs the required secrets that we need to add as secrets in kedacore.

We use this layout:

```
-> modules
    -> azure
        -> .....
    -> github
        -> .....
    -> .....
```

The `root` directory containis the `.tf` files where every module is added and the global variables that every module needs. This part is also responsible of persisting the state in the backend (`azurerm`).

## Prerequisites:

- Terraform >= 1.3
- An Azure Storage Account to be [used as backend](https://www.terraform.io/language/settings/backends/azurerm)
- An [authenticated connection with Azure](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs#authenticating-to-azure)
- A [Github Token or App](https://registry.terraform.io/providers/integrations/github/latest/docs#authentication) with at least `repo.public_repo` and `read:public_key` permissions.
