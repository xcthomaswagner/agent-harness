# Salesforce Implementation Supplement

## Apex

### Class Patterns
- Use `with sharing` by default for security (respects record-level access)
- Use `without sharing` only when explicitly needed (system-level operations)
- Bulkify all triggers and classes — never assume single-record context
- Use `Database.insert(records, false)` for partial success handling in batch operations

### Governor Limits
- **100 SOQL queries** per transaction — avoid queries inside loops
- **150 DML operations** per transaction — collect records, DML once
- **10,000 records** per DML statement
- **6MB heap** for synchronous, **12MB** for async
- **10 seconds** CPU time for synchronous, **60 seconds** for async
- Use `Limits` class to check usage when approaching limits

### SOQL Best Practices
- Always use bind variables: `WHERE Id = :recordId` not string concatenation
- Select only needed fields, never `SELECT *` (not supported anyway)
- Use `WITH SECURITY_ENFORCED` or check FLS manually
- Prefer `Database.query()` with bind variables over dynamic SOQL with string interpolation

### Trigger Pattern
```apex
// Use a trigger handler pattern — no logic in triggers
trigger AccountTrigger on Account (before insert, before update, after insert, after update) {
    AccountTriggerHandler.handle(Trigger.new, Trigger.oldMap, Trigger.operationType);
}
```

### Async Patterns
- **Batch Apex** for processing large data sets (up to 2000 records per chunk)
- **Queueable Apex** for chaining async operations
- **Future methods** for simple async callouts
- **Platform Events** for event-driven architecture

## Lightning Web Components (LWC)

### Component Structure
```
force-app/main/default/lwc/myComponent/
├── myComponent.js          # Component logic
├── myComponent.html        # Template
├── myComponent.css         # Styles
├── myComponent.js-meta.xml # Metadata (targets, visibility)
└── __tests__/
    └── myComponent.test.js # Jest tests
```

### Wire Service
- Use `@wire` for reactive data binding to Apex or UI API
- Wire adapters are cached — use `refreshApex()` to force refresh
- Handle loading and error states in all wired properties

### Apex Integration
- Use `@AuraEnabled(cacheable=true)` for wire-compatible read methods
- Use `@AuraEnabled` (without cacheable) for DML operations
- Always return wrapper classes or primitives, not SObjects directly from LWC context

## B2B Commerce

### Key Objects
- `WebStore`, `WebCart`, `CartItem`, `Product2`, `PricebookEntry`
- `BuyerAccount`, `BuyerGroup`, `BuyerGroupMember`
- `CommerceEntitlementPolicy`

### Commerce APIs
- Cart operations require buyer-authenticated context
- Use `ConnectApi.CommerceCart` for cart manipulation
- Checkout flows use the `sfdc_checkout` namespace

## Agentforce / GenAI

### Metadata Types
- `GenAiPlannerBundle` — reasoning engine container
- `GenAiPlugin` — topic definition
- `GenAiFunction` — action definition
- `GenAiPromptTemplate` — prompt template

### Deployment Order (Critical)
1. Bot / BotVersion
2. GenAiPromptTemplate
3. GenAiFunction
4. GenAiPlugin
5. GenAiPlannerBundle

### Action Best Practices
- Use `@InvocableMethod` with descriptive labels (Atlas reads these)
- Accept `List<>` inputs — Agentforce does NOT auto-bulkify
- Add topic instruction: "If updating more than one record, pass all records to the action for bulk processing"
