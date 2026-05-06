// Set up environment variables for testing
process.env.NODE_ENV = 'test';
process.env.API_KEY = 'test-api-key';
process.env.API_BASE_URL = 'http://localhost:8000';
process.env.LOG_LEVEL = 'error'; // Reduce noise in tests

// Mock Winston logger
jest.mock('winston', () => ({
    format: {
        combine: jest.fn(),
        timestamp: jest.fn(),
        json: jest.fn()
    },
    createLogger: jest.fn(() => ({
        info: jest.fn(),
        error: jest.fn(),
        warn: jest.fn(),
        debug: jest.fn()
    })),
    transports: {
        Console: jest.fn(),
        File: jest.fn()
    }
})); 