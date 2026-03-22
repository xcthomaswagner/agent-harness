# Salesforce Code Conventions

## Project Structure

```
force-app/
  main/
    default/
      classes/           # Apex classes
      triggers/          # Apex triggers
      lwc/               # Lightning Web Components
      aura/              # Aura Components (legacy)
      objects/            # Custom objects and fields
      permissionsets/     # Permission sets
      flows/              # Flows
      genAiFunctions/     # Agentforce actions
      genAiPlugins/       # Agentforce topics
      genAiPlannerBundles/ # Agentforce planners
```

## Naming Conventions

| Item | Convention | Example |
|------|-----------|---------|
| Apex class | PascalCase | `AccountService.cls` |
| Apex test class | PascalCase + `Test` | `AccountServiceTest.cls` |
| Apex trigger | PascalCase + `Trigger` | `AccountTrigger.trigger` |
| Apex method | camelCase | `getAccountById()` |
| Apex variable | camelCase | `accountList` |
| Apex constant | UPPER_SNAKE | `MAX_BATCH_SIZE` |
| LWC component | camelCase (folder) | `lwc/accountCard/` |
| LWC JS property | camelCase | `accountName` |
| Custom object | PascalCase + `__c` | `Invoice__c` |
| Custom field | PascalCase + `__c` | `Total_Amount__c` |
| Custom metadata | PascalCase + `__mdt` | `App_Config__mdt` |

## Apex Style

```apex
public with sharing class AccountService {
    private static final Integer MAX_BATCH_SIZE = 200;

    public static List<Account> getActiveAccounts() {
        return [
            SELECT Id, Name, Industry
            FROM Account
            WHERE IsActive__c = true
            WITH SECURITY_ENFORCED
            ORDER BY Name
            LIMIT 1000
        ];
    }

    public static void updateAccounts(List<Account> accounts) {
        if (accounts == null || accounts.isEmpty()) {
            return;
        }
        Database.SaveResult[] results = Database.update(accounts, false);
        for (Database.SaveResult sr : results) {
            if (!sr.isSuccess()) {
                for (Database.Error err : sr.getErrors()) {
                    System.debug(LoggingLevel.ERROR, 'Update failed: ' + err.getMessage());
                }
            }
        }
    }
}
```

## LWC Style

```javascript
import { LightningElement, api, wire } from 'lwc';
import getAccounts from '@salesforce/apex/AccountService.getActiveAccounts';

export default class AccountList extends LightningElement {
    @api recordId;
    accounts;
    error;

    @wire(getAccounts)
    wiredAccounts({ data, error }) {
        if (data) {
            this.accounts = data;
            this.error = undefined;
        } else if (error) {
            this.error = error;
            this.accounts = undefined;
        }
    }
}
```

## SF CLI Commands

```bash
# Deploy to org
sf project deploy start --source-dir force-app/ --target-org <alias>

# Retrieve from org
sf project retrieve start --metadata ApexClass --target-org <alias>

# Run tests
sf apex run test --code-coverage --result-format human --target-org <alias>

# Query records
sf data query --query "SELECT Id, Name FROM Account LIMIT 5" --target-org <alias>
```
