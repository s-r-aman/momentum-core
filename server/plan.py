# imports needed to run the code in this notebook
import ast
import hashlib
import json
import os
import random
import re
import shlex
import string
import subprocess
from typing import List

import psycopg2
from fastapi import (
    HTTPException,  # used for detecting whether generated Python code is valid
)
from langchain.schema import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from server.endpoint_detection import EndpointManager
from server.parse import get_flow, get_graphical_flow_structure, get_node
from server.projects import ProjectManager
from server.utils.ai_helper import llm_call, print_messages, get_llm_client
from server.utils.github_helper import GithubService

project_manager = ProjectManager()

class TestPlan(BaseModel):
    happy_path: List[str]
    edge_case: List[str]

class Plan:
    def __init__(self, user_id):

        self.openai_client = get_llm_client(
            user_id,
            "gpt-3.5-turbo-0125",
        )
        self.user_pref_openai_client = get_llm_client(
            user_id,
            os.environ["OPENAI_MODEL_REASONING"],
        )
        self.explain_client = self.openai_client
        self.plan_client = self.user_pref_openai_client
        self.test_client = self.user_pref_openai_client

    # example of a function that uses a multi-step prompt to write integration tests
    def explanation_from_function(
        self,
        function_to_test: str,  # Python function to test, as a string
        print_text: bool = True,  # optionally prints text; helpful for understanding the function & debugging
        explain_model: str = "mistral",  # model used to generate text plans in step 1
        # model used to generate code in step 3
        temperature: float = 0.4,  # temperature = 0 can sometimes get stuck in repetitive loops, so we use 0.4
    ) -> str:
        print(function_to_test)
        """Returns a integration test for a given Python function, using a 3-step GPT prompt."""

        # Step 1: Generate an explanation of the function
        explain_system_message = SystemMessage(
            content=(
                "You are a world-class Python developer with an eagle eye for"
                " unintended bugs and edge cases. You carefully explain code"
                " with great detail and accuracy. You organize your"
                " explanations in markdown-formatted, bulleted lists."
            ),
        )
        explain_user_message = HumanMessage(
            content=f"""Please explain the following Python function. Review what each element of the function is doing precisely and what the author's intentions may have been. Organize your explanation as a markdown-formatted, bulleted list.

        ```python
        {function_to_test}
        ```""",
        )

        explain_messages = [explain_system_message, explain_user_message]
        if print_text:
            print_messages(explain_messages)

        explanation = llm_call(self.explain_client, explain_messages)
        return explanation.content

    async def _plan(
        self,
        explanation: str,  # explanation of the function, as a string
        test_package: str = "pytest",  # integration testing package; use the name as it appears in the import statement
        approx_min_cases_to_cover: int = 6,  # minimum number of test case categories to cover (approximate)
        print_text: bool = True,  # optionally prints text; helpful for understanding the function & debugging
    ) -> str:
        explain_assistant_message = AIMessage(content=explanation)

        plan_user_message = HumanMessage(
            content=f"""A good integration test suite should aim to:
    - Test the function's behavior for a wide range of possible inputs
    - Test edge cases that the author may not have foreseen
    - Take advantage of the features of `{test_package}` to make the tests easy to write and maintain
    - Be easy to read and understand, with clean code and descriptive names
    - Be deterministic, so that the tests always pass or fail in the same way
    - Evaluate what scenarios are possible 
    - Reuse code by using fixtures and other testing utilities for common setup and mocks 

    To help integration test the flow above, list diverse scenarios that the function should be able to handle (and under each scenario, include a few examples).
    Include exactly 3 scenario statements of happpy paths and 3 scenarios of edge cases. 
    Format your output in JSON format as such, each scenario is only a string statement:
    {{
    \"happy_path\": [\"happy_scenario0\", \"happy_scenario1\", happy_scenario2,\" happy_scenario3\", \"happy_scenario4\", \"happy_scenario5\"],
    \"edge_case\": [\"edge_scenario1\",\" edge_scenario2\", \"edge_scenario3\"]
    }}

    Ensure that your output is JSON parsable."""
        )
        plan_messages = [
            explain_assistant_message,
            plan_user_message,
        ]
        if print_text:
            print("Plan messages:")
            print_messages(plan_messages)

        plan = llm_call(self.plan_client, plan_messages)
        plan_assistant_message = AIMessage(content=plan.content)

        # Step 2b: If the plan is short, ask GPT to elaborate further
        # this counts top-level bullets (e.g., categories), but not sub-bullets (e.g., test cases)
        plan_content = self._extract_json(plan.content)
        print(plan_content)
        num_scenarios = len(plan_content["happy_path"]) + len(
            plan_content["edge_case"]
        )
        elaboration_needed = num_scenarios < approx_min_cases_to_cover
        if elaboration_needed:
            elaboration_user_message = HumanMessage(
                content=f"""In addition to those scenarios above, list a few rare or unexpected edge cases (and as before, under each edge case, include a few examples as sub-bullets). Follow the same format and ensure that you do not duplicate any scenario. Add these additional scenarios to the edge cases key.""",
            )
            elaboration_messages = [
                explain_assistant_message,
                plan_user_message,
                plan_assistant_message,
                elaboration_user_message,
            ]
            if print_text:
                print_messages([elaboration_user_message])
            elaboration = ""
            if elaboration_needed:
                elaboration = llm_call(self.plan_client, elaboration_messages)
            return elaboration.content
        else:
            return plan

    async def create_temp_test_file(self, identifier, result):
        projects = ProjectManager().list_projects()
        temp_file_id = "".join(
            random.choice(string.ascii_letters) for _ in range(8)
        )
        if not os.path.exists(f"{projects[0]['directory']}/tests"):
            os.mkdir(f"{projects[0]['directory']}/tests")

        filename = f"{projects[0]['directory']}/tests/test_{identifier.split(':')[-1]}_{temp_file_id}.py"

        with open(filename, "w") as file:
            # Write the string to the file
            file.write(result)
        return filename

    async def run_tests(self, identifier, content):
        directory = os.getcwd()
        try:

            test_filepath = await self.create_temp_test_file(
                identifier, content
            )
            test_filename = test_filepath.split("/")[-1]

            if not (
                test_filename.endswith("_test.py")
                or test_filename.startswith("test")
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Invalid test file name. File should start or end with"
                        " 'test.py'."
                    ),
                )

            # Construct the pytest command
            # activate_venv_command = f"source {directory}/.venv/bin/activate"
            pytest_command = (
                "pytest --verbose --json-report --json-report-file=-"
                f" {shlex.quote(test_filepath)}"
            )
            full_command = pytest_command
            # result = subprocess.run(activate_venv_command, shell=True, capture_output=True, text=True, executable='/bin/bash')

            # Execute the pytest command within the virtual environment
            result = subprocess.run(
                full_command,
                shell=True,
                capture_output=True,
                text=True,
                executable="/bin/bash",
            )
            output = result.stdout if result.stdout else result.stderr
            return output
        except Exception as e:
            print(e)

    def _get_explanation_for_function(
        self, function_identifier, node, project_id
    ):
        conn = psycopg2.connect(os.getenv("POSTGRES_SERVER"))
        cursor = conn.cursor()
        if "project_id" in node:
            code = GithubService.fetch_method_from_repo(node)
            code_hash = hashlib.sha256(
                code.encode("utf-8")
            ).hexdigest()
            cursor.execute(
                "SELECT explanation, project_id FROM explanation WHERE"
                " identifier=%s AND hash=%s",
                (function_identifier, code_hash),
            )
            explanation_row = cursor.fetchone()

            if explanation_row:
                explanation = explanation_row[0]
                if explanation_row[1] != project_id:
                    cursor.execute(
                        "INSERT INTO explanation (identifier, hash,"
                        " explanation, project_id) VALUES (%s, %s, %s, %s)",
                        (
                            function_identifier,
                            code_hash,
                            explanation,
                            project_id,
                        ),
                    )
            else:
                code = GithubService.fetch_method_from_repo(
                    node
                )
                explanation = self.explanation_from_function(code)
                cursor.execute(
                    "INSERT INTO explanation (identifier, hash, explanation,"
                    " project_id) VALUES (%s, %s, %s, %s)",
                    (function_identifier, code_hash, explanation, project_id),
                )
                conn.commit()

        return explanation

    def _get_code_for_node(self, node):
        return GithubService.fetch_method_from_repo(node)

    def _extract_json(self, text):
        json_data = None
        pattern = (  # Regular expression pattern to match JSON objects
            r"({[^{}]*})"
        )
        try:
            json_data = json.loads(text)
            print("JSON data extracted successfully")
        except:
            match = re.search(pattern, text)
            if match:
                try:
                    json_data = json.loads(match.group())
                except json.JSONDecodeError as e:
                    print(f"Error decoding JSON: {e}")
        return json_data

    async def generate_tests(
        self,
        plan: str,
        function_to_test: str,
        pydantic_classes: str,
        preferences: dict,
        endpoint_path: str,
        test_package: str = "pytest",
        print_text: bool = True,
        temperature: float = 0.4,
        reruns_if_fail: int = 1,
    ) -> str:
        execute_system_message = SystemMessage(
            content=(
                "You are a world-class Python SDET who specialises in FastAPI,"
                " pytest, pytest-mocks with an eagle eye for unintended bugs"
                " and edge cases. You write careful, accurate integration"
                " tests using the aforementioned frameworks. When asked to"
                " reply only with code, you write all of your code in a single"
                " block."
            ),
        )
        plan_message = AIMessage(content=plan)
        execute_user_message = HumanMessage(content=f"""
    Using Python and the `{test_package}` package along with pytest-mocks for mocking, write a suite of integration tests - one each for every scenario in the test plan above, personalise your tests for the flow defined by the following function code: 
    #         ```python
    #         # flow to test
    #         {function_to_test}
    #         ```
    The complete path of the endpoint is {endpoint_path}. It is important to use this complete path in the test API call because the code might not contain prefixes.

    Consider the following points while writing the integration tests:

    * Review the provided test plan and understand the different test scenarios that need to be covered. Consider edge cases, error handling, and potential variations in input data.
    * Use the provided pydantic classes ({pydantic_classes}) to create the necessary pydantic objects for the test data and mock data setup. This ensures that the tests align with the expected data structures used in the function.
    * Use your judgment to determine which components should be mocked, such as the database and any external API calls. Don't mock internal methods unless specified.
    * Utilize FastAPI TestClient and dependency overrides wherever possible to set up the tests. Create fixtures to minimize code duplication.
    * If there is authorisation involved, mock the authorisation middleware/dependency to always be authenticated. 
    * Do not import any methods from the files in the test case. Only use the Test CLient to test the code through APIs.
    * ALWAYS create a new FastAPI app in the test client and IMPORT THE RELEVANT ROUTERS in it for testing. DO NO TRY to import the main FastAPI app. DO NOT WRITE any new routers in the test file. 
    * Use pytest-mocks library only for mocking. For mocked response objects, use the output structure IF it is defined in the code ELSE infer the expected output structure based on the code and test plan.
    * When defining the target using pytest mocks, ensure that the target path is the path of the call and not the path of the definition.
    For a func_a defined at src.utils.helper and imported in code as from src.utils.helper import func_a, the mock would look like : mocker.patch('src.pipeline.node_1.func_a', return_value="some_value")
    * Write clear and concise test case names that reflect the scenario being tested. Use assertions to validate the expected behavior and handle potential exceptions gracefully.
    * Use appropriate setup and teardown methods to manage test resources efficiently.
    * Reply only with complete code, formatted as follows:
    ```python
    # imports
    import {test_package}  # used for our integration tests
    #insert other imports as needed
    # Any required fixtures can be defined here
    # integration tests
    #insert integration test code here
    ```
    """)
        execute_messages = [
            plan_message,
            execute_system_message,
        ]

        execute_messages += [execute_user_message]
        if print_text:
            print_messages([execute_system_message, execute_user_message])

        execution = llm_call(
            self.test_client, execute_messages, print_text, temperature
        )

        # check the output for errors
        code = execution.content.split("```python")[1].split("```")[0].strip()
        try:
            ast.parse(code)
            # output = await run_tests(code)

        except SyntaxError as e:
            print(f"Syntax error in generated code: {e}")
            if reruns_if_fail > 0:
                print("Rerunning...")
                return await self.generate_tests(
                    plan=plan,
                    function_to_test=function_to_test,
                    pydantic_classes=pydantic_classes,
                    preferences=preferences,
                    endpoint_path=endpoint_path,
                    test_package=test_package,
                    print_text=print_text,
                    temperature=temperature,
                    reruns_if_fail=reruns_if_fail
                    - 1,  # decrement rerun counter when calling again
                )
        # return the integration test as a string
        return code

    async def generate_test_plan_for_endpoint(
        self, identifier: str, project_details: list
    ):
        flow = get_flow(identifier, project_details[2])
        graph = get_graphical_flow_structure(
            identifier, project_details[1], project_details[2]
        )
        if len(flow) == 0:
            raise HTTPException(
                status_code=404, detail="Identifier not found, run code"
            )
        context = ""
        for function in flow:
            node = get_node(function, project_details)
            context += (
                "\n"
                + function
                + "\n code: \n"
                + self._get_code_for_node(node)
                + "\n explanation: \n"
                + self._get_explanation_for_function(
                    function, node, project_details[2]
                )
            )
        context = (
            "The structure of the code represented as a tree in json"
            f" format:\n {graph}"
            + context
        )
        test_plan = await self._plan(context)
        test_plan = self._extract_json(test_plan.content)
        plan_obj = TestPlan(**test_plan)
        (
            EndpointManager( project_details[1]).update_test_plan(
                identifier, plan_obj.model_dump_json(), project_details[2]
            )
        )
        return test_plan
