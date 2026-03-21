# Sitecore QA Supplement

## Testing Sitecore Components

### Unit Tests
- Mock the Sitecore context (`SitecoreContext`) for component tests
- Mock layout data using fixtures that match the Layout Service response shape
- Test that field helper components render (not raw values)
- Test placeholder rendering if the component uses child placeholders

### Mock Data Patterns
```typescript
// Example fixture for a Sitecore component test
const mockFields = {
  title: { value: "Test Title" },
  body: { value: "<p>Test body</p>" },
  image: {
    value: {
      src: "/test-image.jpg",
      alt: "Test",
      width: "800",
      height: "600",
    },
  },
};
```

### Integration Tests
- Test GraphQL queries against a mock GraphQL server or fixtures
- Verify content resolver output matches expected shape
- Test API routes that serve Sitecore data

### E2E Considerations
- Sitecore CM (Content Management) may not be available in test environments
- Use rendering host in disconnected mode for E2E tests
- Experience Editor testing requires a running Sitecore instance (defer to manual QA)

## What NOT to Test
- Sitecore platform behavior (serialization sync, publishing, indexing)
- Layout Service response shape (trust the platform)
- Experience Editor functionality (requires live Sitecore instance)
