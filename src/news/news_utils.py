from textblob import TextBlob

def score_sentiment(headlines):
    scores = []
    for headline in headlines:
        polarity = TextBlob(headline).sentiment.polarity
        scores.append(polarity)
    if scores:
        avg_score = sum(scores) / len(scores)
        if avg_score > 0.1:
            return 1  # Positive
        elif avg_score < -0.1:
            return -1  # Negative
    return 0  # Neutral
