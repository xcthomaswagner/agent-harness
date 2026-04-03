# Salesforce QA Supplement

## Apex Testing

### Test Framework
- Tests use `@IsTest` annotation (not deprecated `testMethod`)
- Use `Test.startTest()` / `Test.stopTest()` to reset governor limits
- Always use `System.assert*` methods with descriptive messages
- Create test data in the test — never rely on org data (`SeeAllData=false`)

### Test Data Patterns
```apex
@IsTest
private class AccountServiceTest {
    @TestSetup
    static void setup() {
        Account acc = new Account(Name = 'Test Account');
        insert acc;
    }

    @IsTest
    static void testUpdateAccount() {
        Account acc = [SELECT Id, Name FROM Account LIMIT 1];
        Test.startTest();
        AccountService.updateName(acc.Id, 'New Name');
        Test.stopTest();

        Account updated = [SELECT Name FROM Account WHERE Id = :acc.Id];
        System.assertEquals('New Name', updated.Name, 'Name should be updated');
    }
}
```

### What to Test
- Positive scenarios (happy path)
- Negative scenarios (invalid input, missing permissions)
- Bulk scenarios (200+ records to verify bulkification)
- Governor limit compliance (use `Test.startTest()` to get fresh limits)
- `with sharing` enforcement (use `System.runAs` to test as restricted user)

### Coverage Requirements
- Minimum 75% code coverage (Salesforce deployment requirement)
- Target 85%+ for quality
- Every trigger must have a test
- Every `@AuraEnabled` method must have a test
- Every `@InvocableMethod` must have a test

### Running Tests
```bash
# Run all tests with coverage
sf apex run test --code-coverage --result-format human --target-org <alias>

# Run specific test class
sf apex run test --tests AccountServiceTest --target-org <alias>

# Run tests matching a pattern
sf apex run test --tests ".*Test" --target-org <alias>
```

## LWC Testing

### Jest Tests
```javascript
import { createElement } from 'lwc';
import MyComponent from 'c/myComponent';
import getRecords from '@salesforce/apex/MyController.getRecords';

jest.mock('@salesforce/apex/MyController.getRecords', () => ({
    default: jest.fn()
}), { virtual: true });

describe('c-my-component', () => {
    afterEach(() => {
        while (document.body.firstChild) {
            document.body.removeChild(document.body.firstChild);
        }
        jest.clearAllMocks();
    });

    it('renders records', async () => {
        getRecords.mockResolvedValue([{ Id: '001', Name: 'Test' }]);
        const element = createElement('c-my-component', { is: MyComponent });
        document.body.appendChild(element);
        await Promise.resolve();
        const items = element.shadowRoot.querySelectorAll('li');
        expect(items.length).toBe(1);
    });
});
```

### LWC Test Commands
```bash
npx lwc-jest --coverage
```

## Agentforce Testing

### Three Testing Approaches

1. **Agent Builder** — manual Conversation Preview + Plan Tracer during development
2. **Testing Center** — automated batch testing in Setup; auto-generates test cases from data libraries
3. **Testing API** — REST endpoint for CI/CD integration:
   ```
   POST /services/data/v63.0/einstein/ai-evaluations/runs
   ```

### CLI Commands
```bash
sf agent generate test-spec --target-org <alias>
sf agent test create --spec-file <path> --target-org <alias>
sf agent test run --target-org <alias>
```

### Evaluation Metrics
When validating Agentforce agents, the platform evaluates:
- **Topic match** — did the agent route to the correct topic?
- **Action sequence match** — did it call the right actions in order?
- **Outcome** — semantic match of the final response
- **Coherence, completeness, conciseness** — response quality
- **Instruction adherence** — did it follow topic instructions?
- **Factuality** — grounded in data, not hallucinated?
- **Latency** — response time

### What to Validate for Agentforce Tickets
- [ ] Topics route correctly for sample utterances
- [ ] Actions execute in correct dependency order
- [ ] Escalation triggers when expected
- [ ] Agent respects data permissions (runs as Agent User, not admin)
- [ ] Error messages are user-friendly (not Apex stack traces)
- [ ] schema.json input/output contracts match what the agent sends

## B2B Commerce QA

- [ ] Cart operations work with buyer-authenticated user
- [ ] Product visibility respects BuyerGroup entitlements
- [ ] Pricing matches the buyer's negotiated price book
- [ ] Checkout flow completes end-to-end (cart → order)
- [ ] Reorder from past orders works (clone + cart)
- [ ] Effective account switching maintains correct pricing context

## OMS QA

- [ ] Cancel Preview shows correct refund amount before Submit
- [ ] Return eligibility window enforced
- [ ] FulfillmentOrder status prevents cancellation when shipped
- [ ] ChangeOrder records created correctly on cancel/return/adjust
- [ ] Refund async operation returns valid operation ID

## What NOT to Test
- Salesforce platform behavior (standard workflow rules, validation rules)
- Declarative automation (flows, process builders) — test via org
- Managed package functionality
- Standard UI behavior
