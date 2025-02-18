from typing import List, Optional, Union

import og_dspy
from og_dsp.utils import dotdict
from og_dspy.primitives.prediction import Prediction

try:
    import weaviate
except ImportError as err:
    raise ImportError(
        "The 'weaviate' extra is required to use WeaviateRM. Install it with `pip install dspy-ai[weaviate]`",
    ) from err


class WeaviateRM(og_dspy.Retrieve):
    """A retrieval module that uses Weaviate to return the top passages for a given query.

    Assumes that a Weaviate collection has been created and populated with the following payload:
        - content: The text of the passage

    Args:
        weaviate_collection_name (str): The name of the Weaviate collection.
        weaviate_client (WeaviateClient): An instance of the Weaviate client.
        k (int, optional): The default number of top passages to retrieve. Default to 3.

    Examples:
        Below is a code snippet that shows how to use Weaviate as the default retriever:
        ```python
        import weaviate

        llm = og_dspy.Cohere(model="command-r-plus", api_key=api_key)
        weaviate_client = weaviate.connect_to_[local, wcs, custom, embedded]("your-path-here")
        retriever_model = WeaviateRM("my_collection_name", weaviate_client=weaviate_client)
        og_dspy.settings.configure(lm=llm, rm=retriever_model)

        retrieve = og_dspy.Retrieve(k=1)
        topK_passages = retrieve("what are the stages in planning, sanctioning and execution of public works").passages
        ```

        Below is a code snippet that shows how to use Weaviate in the forward() function of a module
        ```python
        self.retrieve = WeaviateRM("my_collection_name", weaviate_client=weaviate_client, k=num_passages)
        ```
    """

    def __init__(
        self,
        weaviate_collection_name: str,
        weaviate_client: Union[weaviate.WeaviateClient, weaviate.Client],
        weaviate_collection_text_key: Optional[str] = "content",
        k: int = 3,
    ):
        self._weaviate_collection_name = weaviate_collection_name
        self._weaviate_client = weaviate_client
        self._weaviate_collection_text_key = weaviate_collection_text_key

        # Check the type of weaviate_client (this is added to support v3 and v4)
        if hasattr(weaviate_client, "collections"):
            self._client_type = "WeaviateClient"
        elif hasattr(weaviate_client, "query"):
            self._client_type = "Client"
        else:
            raise ValueError("Unsupported Weaviate client type")

        super().__init__(k=k)

    def forward(self, query_or_queries: Union[str, List[str]], k: Optional[int] = None, **kwargs) -> Prediction:
        """Search with Weaviate for self.k top passages for query or queries.

        Args:
            query_or_queries (Union[str, List[str]]): The query or queries to search for.
            k (Optional[int]): The number of top passages to retrieve. Defaults to self.k.
            kwargs :

        Returns:
            og_dspy.Prediction: An object containing the retrieved passages.
        """
        k = k if k is not None else self.k
        queries = [query_or_queries] if isinstance(query_or_queries, str) else query_or_queries
        queries = [q for q in queries if q]
        passages, parsed_results = [], []
        for query in queries:
            if self._client_type == "WeaviateClient":
                results = self._weaviate_client.collections.get(self._weaviate_collection_name).query.hybrid(
                    query=query,
                    limit=k,
                    **kwargs,
                )

                parsed_results = [result.properties[self._weaviate_collection_text_key] for result in results.objects]

            elif self._client_type == "Client":
                results = (
                    self._weaviate_client.query.get(
                        self._weaviate_collection_name,
                        [self._weaviate_collection_text_key],
                    )
                    .with_hybrid(query=query)
                    .with_limit(k)
                    .do()
                )

                results = results["data"]["Get"][self._weaviate_collection_name]
                parsed_results = [result[self._weaviate_collection_text_key] for result in results]

            passages.extend(dotdict({"long_text": d}) for d in parsed_results)

        return passages
