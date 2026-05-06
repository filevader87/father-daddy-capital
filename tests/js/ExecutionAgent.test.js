const axios = require('axios');
const ExecutionAgent = require('../../ExecutionAgent');

// Mock axios
jest.mock('axios');

describe('ExecutionAgent', () => {
    let agent;
    
    beforeEach(() => {
        // Clear all mocks before each test
        jest.clearAllMocks();
        agent = ExecutionAgent;
    });

    test('should execute trade successfully', async () => {
        const mockResponse = {
            data: {
                tradeId: '123',
                status: 'success',
                asset: 'BTC',
                amount: 1000
            }
        };

        axios.create.mockReturnValue({
            post: jest.fn().mockResolvedValue(mockResponse)
        });

        const result = await agent.executeTrade('BTC', 1000);
        
        expect(result).toEqual(mockResponse.data);
        expect(agent.axiosInstance.post).toHaveBeenCalledWith(
            '/api/trading/execute',
            { asset: 'BTC', amount: 1000 },
            { headers: { 'x-api-key': expect.any(String) } }
        );
    });

    test('should handle trade execution failure', async () => {
        const mockError = new Error('API Error');
        
        axios.create.mockReturnValue({
            post: jest.fn().mockRejectedValue(mockError)
        });

        const result = await agent.executeTrade('BTC', 1000);
        
        expect(result.status).toBe('error');
        expect(result.message).toBe('API Error');
        expect(result.context.errorType).toBe('EXECUTION_ERROR');
    });

    test('should retry failed trades', async () => {
        const mockError = new Error('Temporary failure');
        const mockSuccess = {
            data: {
                tradeId: '123',
                status: 'success'
            }
        };

        const mockPost = jest.fn()
            .mockRejectedValueOnce(mockError)
            .mockRejectedValueOnce(mockError)
            .mockResolvedValueOnce(mockSuccess);

        axios.create.mockReturnValue({ post: mockPost });

        const result = await agent.executeTrade('BTC', 1000);
        
        expect(mockPost).toHaveBeenCalledTimes(3);
        expect(result).toEqual(mockSuccess.data);
    });

    test('should validate trade parameters before execution', async () => {
        const result = await agent.executeTrade('INVALID', 1000);
        
        expect(result.status).toBe('error');
        expect(result.message).toContain('Unsupported asset: INVALID');
        expect(axios.create().post).not.toHaveBeenCalled();
    });
}); 