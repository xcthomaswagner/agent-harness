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
- Batch Apex chunk sizes: default 200, max 2000

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
- **Queueable Apex** for chaining async operations or long-running callouts
- **Future methods** for simple async callouts (no chaining, no complex types)
- **Platform Events** for event-driven architecture and cross-system integration

## Lightning Web Components (LWC)

### Component Structure
```
force-app/main/default/lwc/myComponent/
├── myComponent.js          # Component logic
├── myComponent.html        # Template
├── myComponent.css         # Styles (scoped)
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

### Experience Cloud / LWR Sites
- LWR (Lightning Web Runtime) sites use LWC components with `@api` properties exposed via Experience Builder
- Use `lightningCommunity__Page`, `lightningCommunity__Default` targets in `.js-meta.xml`
- Access current user via `@salesforce/user/Id`, community context via `@salesforce/community/Id`
- Navigation: use `NavigationMixin` — `this[NavigationMixin.Navigate]` for page navigation
- Guest user access: components must handle unauthenticated state gracefully

## Agentforce / GenAI Development

### Metadata Types & Source Structure
```
force-app/main/default/
├── bots/<AgentName>/
│   ├── <AgentName>.bot-meta.xml
│   └── v1/v1.botVersion-meta.xml
├── genAiPlannerBundles/<Name>/
│   ├── <Name>.genAiPlannerBundle-meta.xml
│   ├── <Name>.agent                    # Agent Script file (API v65+)
│   └── localActions/<Action>/
│       ├── input/schema.json
│       └── output/schema.json
├── genAiPlugins/<Topic>.genAiPlugin-meta.xml
├── genAiFunctions/<Action>/
│   ├── <Action>.genAiFunction-meta.xml
│   ├── input/schema.json
│   └── output/schema.json
└── genAiPromptTemplates/<Template>.genAiPromptTemplate-meta.xml
```

### Deployment Order (Critical)
1. Bot / BotVersion
2. GenAiPromptTemplate
3. GenAiFunction (dependencies must exist on target)
4. GenAiPlugin (referenced functions must exist)
5. GenAiPlannerBundle (references plugins and functions)
6. Activate BotVersion in production (manual — cannot deploy via metadata API)

### Deployment Gotchas
- `schema.json` changes are **ignored without accompanying XML changes** — always touch the GenAiFunction XML when changing schemas
- Formatting-only `.json` changes won't deploy
- Custom topics based on default topics embed in GenAiPlannerBundle — won't appear as separate GenAiPlugin
- Hardcoded IDs break cross-org deployments
- Set `"sourceApiVersion": "60.0"` minimum in `sfdx-project.json`

### Apex Actions for Agentforce
```apex
public class GetAccountInfo {
    @InvocableMethod(label='Get Account Information'
                     description='Retrieves account details by name or ID')
    public static List<Output> execute(List<Input> inputs) {
        // Accept List<> — Agentforce does NOT auto-bulkify
        List<Output> results = new List<Output>();
        for (Input inp : inputs) {
            try {
                Account acc = [SELECT Id, Name, Industry, AnnualRevenue
                              FROM Account
                              WHERE Id = :inp.accountId
                              WITH SECURITY_ENFORCED
                              LIMIT 1];
                results.add(new Output(acc.Name, acc.Industry, acc.AnnualRevenue));
            } catch (Exception e) {
                // Return user-friendly error, not stack trace
                results.add(new Output('Error: ' + e.getMessage()));
            }
        }
        return results;
    }

    public class Input {
        @InvocableVariable(label='Account ID' required=true
                          description='The 18-character Salesforce Account ID')
        public String accountId;
    }

    public class Output {
        @InvocableVariable(label='Account Name')
        public String name;
        @InvocableVariable(label='Industry')
        public String industry;
        @InvocableVariable(label='Annual Revenue')
        public Decimal revenue;
        @InvocableVariable(label='Error Message')
        public String errorMessage;

        public Output(String name, String industry, Decimal revenue) {
            this.name = name;
            this.industry = industry;
            this.revenue = revenue;
        }
        public Output(String error) {
            this.errorMessage = error;
        }
    }
}
```

Key points:
- Descriptive labels on `@InvocableMethod` and `@InvocableVariable` — Atlas reads these for planning
- Use `try-catch` returning user-friendly error messages in output class
- Use `Database` class for partial success handling
- Use `with sharing` and user mode for security
- Decompose complex actions to avoid CPU timeout
- Use Queueable Apex for long-running operations + requestId for status polling

### Writing Topic Instructions
- Use positive framing ("always verify the account" not "don't skip verification")
- **Avoid "must," "never," "always"** — agent gets stuck on absolute directives
- Start minimal; add complexity incrementally
- Put deterministic business rules in action logic (Flow/Apex), NOT instructions
- Use unique action names — "Locate Project Details" not "Get Project Details"
- Document dependent actions: "Run IdentifyCustomerAccount before RetrieveBillingHistory"

### Agent Script (DSL — API v65+)
```
topic [name]:
  instructions: ->
    | Natural language prompt text
    | Use {!@variables.varName} for interpolation
  reasoning:
    reasoning.instructions -> prompt text
    reasoning.actions -> tools the LLM can invoke
  actions:
    target: "flow://[FlowName]"
    with [param] = ...
    available when [condition]

if @variables.[condition]:
  transition to @topic.[otherTopic]
else:
  @utils.escalate
```
Key references: `@variables.`, `@actions.`, `@topic.`, `@outputs.`, `@utils.escalate`, `@utils.transition to`

### Agent Limits
- 20 agents max per org
- 15 topics max per agent (recommend ~10)
- 15 actions max per topic (recommend 12-15)
- 500 RPM per org per REST endpoint for LLM generation
- 65,536 tokens context window (with data masking enabled)
- 120-second timeout per Agent API request

## B2B Commerce

### Key Objects
- `WebStore`, `WebCart`, `CartItem`, `Product2`, `PricebookEntry`
- `BuyerAccount`, `BuyerGroup`, `BuyerGroupMember`
- `CommerceEntitlementPolicy`
- `ContactPointAddress`, `ContactPointEmail`, `ContactPointPhone`

### Commerce APIs
- Cart operations require buyer-authenticated context (not service account)
- Use `ConnectApi.CommerceCart` for cart manipulation
- Checkout flows use the `sfdc_checkout` namespace
- Checkout PATCH API doesn't accept `deliveryGroups` or `paymentInfo` — use sObject API
- Delivery methods are calculated dynamically by shipping integration during checkout

### B2B Agent Gotchas
- `WebstoreId` is hardcoded in Commerce Global Instructions — update per store
- Agent User permissions must include relevant price books and entitlement policies
- Agent respects BuyerGroup membership — wrong permissions = invisible products/prices
- Product search quality depends on Commerce search index configuration

## OMS (Order Management)

### Object Model
```
OrderSummary
  ├── OrderItemSummary (1:many)
  ├── OrderDeliveryGroupSummary (1:many)
  │   └── FulfillmentOrder (1:many)
  │       └── FulfillmentOrderLineItem (1:many)
  ├── ChangeOrder (1:many)
  ├── ReturnOrder (1:many)
  │   └── ReturnOrderLineItem (1:many)
  ├── OrderPaymentSummary (1:many)
  └── Shipment (via FulfillmentOrder)
```

### Preview/Submit Pattern
Many OMS actions use a two-step pattern:
1. **Preview** — calculates financial impact without committing
2. **Submit** — executes operation, creates ChangeOrder

### ConnectApi
`ConnectApi.OrderSummary` methods: `adjustPreview()`, `adjustSubmit()`, `cancelPreview()`, `cancelSubmit()`, `returnPreview()`, `returnSubmit()`, `createFulfillmentOrders()`

REST base: `/services/data/vXX.0/commerce/order-management/`

## Service Cloud Agents

### Agentforce Service Agent
- Runs as `EinsteinServiceAgent` system user (not per-user)
- Must always configure an **Escalation topic**
- Standard actions: Answer Questions with Knowledge, Query Records, Summarize Record, Get Activities Timeline

### Escalation Pattern
1. Topic instructions define when to escalate
2. Agent triggers escalation action (Omni-Channel Flow)
3. Flow updates `MessagingSession` with escalation reason
4. Omni-Channel routes to human agent (skills/queue based)

### MIAW Deployment (Messaging for In-App and Web)
1. Enable Omni-Channel Settings
2. Create Queue supporting `Messaging Session`
3. Build Omni-Channel Flow (variable `recordId` — **case-sensitive**)
4. Route Work element to "Agentforce Service Agent"
5. Configure Messaging Channel (type: MIAW, routing: Omni-Flow)
6. Deploy via Embedded Service Deployments to storefront
