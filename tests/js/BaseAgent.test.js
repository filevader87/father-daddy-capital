const BaseAgent = require('../../BaseAgent');

describe('BaseAgent', () => {
    let agent;

    beforeEach(() => {
        agent = new BaseAgent('TestAgent');
    });

    test('should initialize with correct name and status', () => {
        expect(agent.name).toBe('TestAgent');
        expect(agent.status).toBe('initializing');
    });

    test('should validate trade parameters correctly', async () => {
        // Valid trade
        await expect(agent.validateTradeParams('BTC', 1000)).resolves.toBe(true);

        // Invalid asset
        await expect(agent.validateTradeParams('INVALID', 1000))
            .rejects
            .toThrow('Unsupported asset: INVALID');

        // Amount too low
        await expect(agent.validateTradeParams('BTC', 50))
            .rejects
            .toThrow('Invalid trade amount');

        // Amount too high
        await expect(agent.validateTradeParams('BTC', 1000000))
            .rejects
            .toThrow('Invalid trade amount');
    });

    test('should emit health check events', (done) => {
        agent.on('healthCheck', (data) => {
            expect(data.status).toBe('healthy');
            expect(data.timestamp).toBeDefined();
            done();
        });

        agent.performHealthCheck();
    });

    test('should handle errors correctly', (done) => {
        const testError = new Error('Test error');
        const context = { test: 'context' };

        agent.on('error', (data) => {
            expect(data.error).toBe(testError);
            expect(data.context).toBe(context);
            done();
        });

        agent.handleError(testError, context);
    });

    test('should return correct status', () => {
        const status = agent.getStatus();
        expect(status).toEqual({
            name: 'TestAgent',
            status: expect.any(String),
            lastHealthCheck: expect.any(Number),
            uptime: expect.any(Number)
        });
    });
}); 