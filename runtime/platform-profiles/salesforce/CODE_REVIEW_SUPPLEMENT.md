# Salesforce Code Review Supplement

## Critical Checks

### Security
- [ ] **Sharing model**: Classes use `with sharing` unless `without sharing` is explicitly justified
- [ ] **FLS/CRUD**: `WITH SECURITY_ENFORCED` or `Schema.SObjectType` checks before DML
- [ ] **SOQL injection**: All queries use bind variables, no string concatenation with user input
- [ ] **No hardcoded IDs**: Record IDs, profile IDs, org IDs not hardcoded (use Custom Settings/Metadata)
- [ ] **Sensitive data**: No credentials, tokens, or PII in debug logs
- [ ] **Guest user access**: Components degrade gracefully when unauthenticated (Experience Cloud)
- [ ] **`@AuraEnabled` validation**: Input validated before processing

### Governor Limits
- [ ] **No SOQL in loops**: Queries are outside loop bodies
- [ ] **No DML in loops**: Records collected then DML'd once
- [ ] **Bulkified**: Code handles 200+ records in trigger context
- [ ] **Heap usage**: No unnecessary large collections or string concatenation in loops
- [ ] **Async appropriate**: Batch (large data), Queueable (chaining), Future (simple callout)

### Trigger Best Practices
- [ ] **No logic in triggers**: All logic delegated to handler classes
- [ ] **Recursion guard**: Static variable prevents infinite trigger recursion
- [ ] **Context-aware**: Handles all relevant contexts (before/after, insert/update/delete)

## Warning Checks

### LWC
- [ ] **Wire error handling**: All `@wire` properties handle loading and error states
- [ ] **Cacheable**: Read-only Apex uses `@AuraEnabled(cacheable=true)`
- [ ] **No DOM manipulation**: Uses reactive properties, not `querySelector` for data binding
- [ ] **Event propagation**: Correct pattern (bubbles + composed when crossing shadow DOM)
- [ ] **CSS scoped**: No global selectors leaking outside component

### Agentforce / GenAI
- [ ] **`@InvocableMethod`**: Has descriptive `label` and `description` (Atlas reads these for planning)
- [ ] **`@InvocableVariable`**: Has descriptive `label` and `description`
- [ ] **Bulkified actions**: Accepts `List<>` inputs — Agentforce does NOT auto-bulkify
- [ ] **Error handling**: Returns user-friendly messages, not stack traces
- [ ] **schema.json changes**: Accompanied by XML metadata changes (schema-only changes silently ignored on deploy)
- [ ] **Deployment order**: Bot → PromptTemplate → Function → Plugin → PlannerBundle
- [ ] **No hardcoded IDs**: Agent/topic references use names, not 18-char IDs
- [ ] **Topic instructions**: Positive framing, no absolute directives ("must"/"never"/"always")
- [ ] **sourceApiVersion**: >= 60.0 in `sfdx-project.json`

### B2B Commerce
- [ ] **WebstoreId**: Not hardcoded — parameterized per store
- [ ] **Buyer context**: Cart operations use buyer-authenticated context, not service account
- [ ] **Entitlement respect**: BuyerGroup membership checked for pricing visibility
- [ ] **Checkout**: Uses sObject API for delivery/payment (PATCH API doesn't accept these)

### OMS
- [ ] **Preview/Submit pattern**: Used for cancel/return/adjust (not direct DML)
- [ ] **FulfillmentOrder status**: Checked before cancellation (only Draft/Assigned allowed)
- [ ] **Business rules**: Return windows, refund policies in action logic, not hardcoded

### General
- [ ] **Test assertions**: Tests have meaningful assertions, not just coverage
- [ ] **Naming conventions**: camelCase methods, PascalCase classes, UPPER_SNAKE constants
- [ ] **API version**: Components and classes use 60.0+
