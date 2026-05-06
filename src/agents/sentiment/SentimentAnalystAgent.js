// agents/SentimentAnalystAgent.js - Fetches market data using the WealthWizard API
const axios = require('axios');
const BaseAgent = require('./BaseAgent');
const config = require('./config/config');

class SentimentAnalystAgent extends BaseAgent {
    constructor() {
        super('SentimentAnalystAgent');
        this.axiosInstance = axios.create({
            baseURL: config.api.baseUrl,
            timeout: config.api.timeout
        });
        this.lastAnalysis = null;
    }

    async getMarketData() {
        try {
            let attempt = 0;
            while (attempt < config.api.retries) {
                try {
                    const response = await this.axiosInstance.get('/api/market/prices', {
                        headers: { 'x-api-key': process.env.API_KEY }
                    });

                    this.lastAnalysis = {
                        timestamp: Date.now(),
                        data: response.data
                    };

                    this.logger.info('Market data retrieved successfully', {
                        dataPoints: response.data.length,
                        timestamp: this.lastAnalysis.timestamp
                    });

                    this.emit('marketDataUpdated', {
                        success: true,
                        data: response.data
                    });

                    return response.data;
                } catch (error) {
                    attempt++;
                    if (attempt === config.api.retries) {
                        throw error;
                    }
                    this.logger.warn(`Market data retrieval attempt ${attempt} failed, retrying...`, {
                        error: error.message
                    });
                    await new Promise(resolve => setTimeout(resolve, 1000 * attempt));
                }
            }
        } catch (error) {
            const errorContext = {
                lastSuccessfulAnalysis: this.lastAnalysis ? this.lastAnalysis.timestamp : null,
                errorType: error.response ? 'API_ERROR' : 'RETRIEVAL_ERROR'
            };
            this.handleError(error, errorContext);

            this.emit('marketDataUpdated', {
                success: false,
                error: error.message
            });

            return {
                status: 'error',
                message: error.message,
                context: errorContext
            };
        }
    }

    async analyzeSentiment(marketData) {
        try {
            // Implement sentiment analysis logic here
            const sentiment = await this.calculateSentiment(marketData);
            
            this.logger.info('Sentiment analysis completed', {
                sentiment,
                timestamp: Date.now()
            });

            this.emit('sentimentAnalyzed', {
                success: true,
                sentiment
            });

            return sentiment;
        } catch (error) {
            this.handleError(error, { context: 'sentiment-analysis' });
            
            this.emit('sentimentAnalyzed', {
                success: false,
                error: error.message
            });

            return {
                status: 'error',
                message: error.message
            };
        }
    }

    async calculateSentiment(marketData) {
        // Placeholder for sentiment calculation logic
        // This should be implemented based on your specific requirements
        return {
            overall: 'neutral',
            score: 0,
            timestamp: Date.now()
        };
    }
}

// Create and export a singleton instance
const sentimentAnalyst = new SentimentAnalystAgent();
module.exports = sentimentAnalyst;
