# Salesforce QA Supplement

## Apex Testing

### Test Framework
- Tests use `@IsTest` annotation
- Use `Test.startTest()` / `Test.stopTest()` to reset governor limits
- Always use `System.assert*` methods with descriptive messages
- Create test data in the test — never rely on org data (`SeeAllData=false`)

### Test Data Patterns
```apex
@IsTest
private class AccountServiceTest {
    @TestSetup
    static void setup() {
        // Create test data once for all test methods
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

### Coverage Requirements
- Minimum 75% code coverage (Salesforce deployment requirement)
- Target 85%+ for quality
- Every trigger must have a test
- Every `@AuraEnabled` method must have a test

## LWC Testing

### Jest Tests
```javascript
import { createElement } from 'lwc';
import MyComponent from 'c/myComponent';
import getRecords from '@salesforce/apex/MyController.getRecords';

// Mock Apex
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

        await Promise.resolve(); // Wait for wire
        const items = element.shadowRoot.querySelectorAll('li');
        expect(items.length).toBe(1);
    });
});
```

### LWC Test Commands
```bash
# Run LWC Jest tests
sfdx force:lightning:lwc:test:run
# or
npx lwc-jest --coverage
```

## What NOT to Test
- Salesforce platform behavior (standard workflow rules, validation rules)
- Declarative automation (flows, process builders) — test via org
- Managed package functionality
- Standard UI behavior
