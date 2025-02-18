import og_dspy


class AnswerCorrectnessSignature(dspy.Signature):
    """Verify that the predicted answer matches the gold answer."""

    question = og_dspy.InputField()
    gold_answer = og_dspy.InputField(desc="correct answer for question")
    predicted_answer = og_dspy.InputField(desc="predicted answer for question")
    is_correct = og_dspy.OutputField(desc='True or False')

class AnswerCorrectness(dspy.Module):
    def __init__(self):
        super().__init__()
        self.evaluate_correctness = og_dspy.ChainOfThought(AnswerCorrectnessSignature)

    def forward(self, question, gold_answer, predicted_answer):
        return self.evaluate_correctness(question=question, gold_answer=gold_answer, predicted_answer=predicted_answer)


class AnswerFaithfulnessSignature(dspy.Signature):
    """Verify that the predicted answer is based on the provided context."""

    context = og_dspy.InputField(desc="relevant facts for producing answer")
    question = og_dspy.InputField()
    answer = og_dspy.InputField(desc="often between 1 and 5 words")
    is_faithful = og_dspy.OutputField(desc='True or False')

class AnswerFaithfulness(dspy.Module):
    def __init__(self):
        super().__init__()
        self.evaluate_faithfulness = og_dspy.ChainOfThought(AnswerFaithfulnessSignature)

    def forward(self, context, question, answer):
        return self.evaluate_faithfulness(context=context, question=question, answer=answer)
