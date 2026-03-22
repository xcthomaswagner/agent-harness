# Salesforce Code Review Supplement

## Critical Checks

### Security
- [ ] **SOQL injection**: All queries use bind variables, no string concatenation with user input
- [ ] **FLS/CRUD checks**: `WITH SECURITY_ENFORCED` or `Schema.SObjectType` checks before DML
- [ ] **Sharing model**: Classes use `with sharing` unless `without sharing` is explicitly justified
- [ ] **No hardcoded IDs**: Record IDs, profile IDs, org IDs not hardcoded (use Custom Settings/Metadata)
- [ ] **Sensitive data**: No credentials, tokens, or PII in debug logs (`System.debug`)

### Governor Limits
- [ ] **No SOQL in loops**: Queries are outside loop bodies
- [ ] **No DML in loops**: Records collected then DML'd once
- [ ] **Bulkified**: Code handles 200+ records in trigger context
- [ ] **Heap usage**: No unnecessary large collections or string concatenation in loops

### Trigger Best Practices
- [ ] **No logic in triggers**: All logic delegated to handler classes
- [ ] **Recursion guard**: Static variable prevents infinite trigger recursion
- [ ] **Context-aware**: Handles all relevant trigger contexts (before/after, insert/update/delete)

## Warning Checks

### LWC
- [ ] **Wire error handling**: All `@wire` properties handle loading and error states
- [ ] **Cacheable**: Read-only Apex uses `@AuraEnabled(cacheable=true)`
- [ ] **No DOM manipulation**: Uses reactive properties, not `querySelector` for data binding

### General
- [ ] **Test assertions**: Tests have actual assertions, not just coverage
- [ ] **Naming conventions**: camelCase for Apex methods, PascalCase for classes
- [ ] **API version**: Components and classes use a current API version (60.0+)

### B2B Commerce
- [ ] **Entitlement respect**: Code checks buyer group membership and entitlement policies
- [ ] **Price book access**: Correct price book used for the buyer context
- [ ] **WebstoreId**: Not hardcoded in commerce code

### Agentforce
- [ ] **schema.json changes**: Accompanied by XML metadata changes (schema-only changes are silently ignored)
- [ ] **Deployment order**: Metadata deployed in correct dependency order
- [ ] **No hardcoded IDs**: Agent and topic references use names, not 18-char IDs
